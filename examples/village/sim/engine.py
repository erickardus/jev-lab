"""The simulation loop: movement, needs, arrivals, conversations, and asking the brain.

Runs on a fixed step. Decisions are asynchronous: when villagers become idle they are
batched into one brain request per tick, and the world keeps moving while it is in
flight. Nothing here blocks on a model.
"""

from __future__ import annotations

import asyncio
import time

from typesafe_sdk import AsyncTypeSafeClient

from . import incidents as inc_mod
from .actions import Action, build_actions, talk_action
from .brain import Brain, Decision, RandomBrain
from .narrator import Narrator, TemplateNarrator
from .npc import NPC, default_cast, traveler
from .outcomes import Outcome, Proposed, judge, judge_traveler
from . import reflection
from .world import VISION, World

CONVERSATION_SECONDS = 14.0
LINE_SECONDS = 3.5
MIN_REQUEST_GAP = 0.6  # real seconds between brain requests, so a burst of idle NPCs is one request
EVENT_EVERY = (45.0, 90.0)  # sim seconds between village events (random in range)
RAIN_SECONDS = 60.0
SHARE_NEWS = 0.5  # p(would pass on news) above which a conversation spreads it
AFFINITY_PER_CHAT = 0.2

INCIDENT_CHANCE = 0.4  # share of events that are incidents (jobs) rather than plain news
PROMISE_SECONDS = 100.0  # a promise must be kept within this many sim seconds (~6.5 game hours)
REMEMBER_SEEN_SECONDS = 120.0  # "last seen" older than this no longer makes someone findable
CHAT_IDLE_SECONDS = 30.0  # a player chat ends after this long without a message
CHAT_REACH = 2.5  # tiles

EVENTS = [
    ("rain", "it has started raining"),
    ("well", "the well's bucket rope snapped"),
    ("fox", "a fox got into the henhouse behind the bakery"),
    ("boat", "a boat was seen on the river at dawn"),
    ("cart", "a merchant's cart broke an axle on the road"),
    ("tavern", "the tavern is out of ale"),
]
EVENT_WITNESS = {"well": "well", "fox": "bakery", "tavern": "tavern", "cart": "market", "boat": "field", "rain": None}


class Sim:
    def __init__(self, brain: Brain, narrator: Narrator | None = None, seed: int = 7) -> None:
        self.world = World(seed)
        self.npcs: list[NPC] = default_cast()
        self.world.npcs = self.npcs
        self.by_id = {n.id: n for n in self.npcs}
        self.world.by_id = self.by_id
        self.player: NPC | None = None
        self._next_event = self.world.rng.uniform(*EVENT_EVERY)
        self._rain_until = 0.0
        self.incidents: list[inc_mod.Incident] = []
        self._n_inc = 0
        self._n_commit = 0
        self._judge: AsyncTypeSafeClient | None = None  # Jev, for verifying conversation outcomes
        # player chat: who they're talking to, the queued message while walking over, activity time
        self.chat_with: str | None = None
        self.chat_queue: list[str] = []
        self.chat_last: float = 0.0
        self.chat_status: str = ""  # walking | talking | thinking
        self.brain = brain
        self.fallback = RandomBrain()
        self.narrator = narrator or TemplateNarrator()
        self.actions: dict[str, Action] = {a.id: a for a in build_actions()}
        self.t = 0.0
        self.speed = 1.0
        self.paused = False
        self._last_request = 0.0
        self._inflight: asyncio.Task | None = None
        self._bubbles: list[tuple[float, NPC, str]] = []  # (show_at, npc, text)
        for n in self.npcs:  # everyone starts at home
            n.x, n.y = self.world.places[n.home].anchor
            n.at_place = n.home
            self.world.places[n.home].owner = n.id
            n.original_traits = list(n.traits)
            n.stamp = lambda: (self.world.day, self.world.clock.hhmm())
        self._reflected: set[tuple[str, int]] = set()

    @property
    def villagers(self) -> list[NPC]:
        return [n for n in self.npcs if not n.controlled]

    def judge_client(self) -> AsyncTypeSafeClient:
        if self._judge is None:
            self._judge = getattr(self.brain, "client", None) or AsyncTypeSafeClient()
        return self._judge

    # ------------------------------------------------------------------ options

    def options_for(self, npc: NPC) -> list[Action]:
        acts = [a for a in self.actions.values() if a.available(npc, self.world)]
        for other in self.npcs:
            if other is not npc:
                ta = self._talk_action(npc, other)
                if ta is not None:
                    acts.append(ta)
        for inc in self.incidents:
            ja = inc_mod.job_action(inc, self)
            if ja.available(npc, self.world):
                acts.append(ja)
        for c in npc.commitments:
            acts.append(self._promise_action(npc, c))
        return acts

    def _talk_action(self, npc: NPC, other: NPC) -> Action | None:
        """Talk to someone you can see, or go looking where you last saw them."""
        base = talk_action(other)
        if not base.available(npc, self.world):
            return None
        visible = other.id in npc.sees
        seen = npc.last_seen.get(other.id)
        if not visible and (seen is None or self.t - seen[2] > REMEMBER_SEEN_SECONDS):
            return None
        if visible:
            return base
        where, hhmm, _ = seen
        place = next((p for p in self.world.places.values() if p.name == where or (where == "home" and p.id == other.home)), None)
        if place is None:
            return None

        def describe(n: NPC, w: World) -> str:
            return f"Go looking for {other.name} the {other.role} where they were last seen ({where}, around {hhmm}; {w.distance_word(n.tile, place.anchor)}) and have a chat"

        def start(n: NPC, w: World) -> None:
            n.target = place.anchor
            n.talking_to = other.id

        return Action(base.id, f"looking for {other.name}", describe, lambda n, w: True, start, base.on_arrive, base.on_complete)

    def _promise_action(self, npc: NPC, c: dict) -> Action:
        to = self.by_id[c["to"]]

        def describe(n: NPC, w: World) -> str:
            left = max(0, c["deadline_t"] - self.t)
            urgency = "soon" if left < PROMISE_SECONDS / 3 else "today"
            return f"Keep the promise to {to.name}: {c['what']} (go find them; {urgency})"

        def start(n: NPC, w: World) -> None:
            n.target = to.tile if (to.id in n.sees or to.controlled) else self.world.places[to.home].anchor
            n._promise = c["id"]

        def on_arrive(n: NPC, w: World, s) -> None:
            if abs(n.x - to.x) + abs(n.y - to.y) <= 3:
                n.busy_until = s.t + 4.0
            else:
                n.remember(f"went to {to.name} to keep a promise but couldn't find them")
                n.busy_until = s.t + 0.1
                n._promise = None

        def on_complete(n: NPC, w: World, s) -> None:
            if getattr(n, "_promise", None) != c["id"]:
                return
            n._promise = None
            n.commitments = [x for x in n.commitments if x["id"] != c["id"]]
            n.remember(f"kept my promise to {to.name}: {c['what']}", significant=True)
            to.remember(f"{n.name} kept their promise: {c['what']}", significant=True)
            to.affinity[n.id] = min(1.0, to.affinity.get(n.id, 0.0) + 0.35)
            n.mood = min(2.0, n.mood + 0.3)
            s.world.log(f"{n.name} keeps a promise to {to.name}: {c['what']}")

        return Action(f"keep_{c['id']}", f"keeping a promise to {to.name}", describe, lambda n, w: True, start, on_arrive, on_complete)

    # --------------------------------------------------------------------- tick

    def tick(self, dt: float) -> None:
        if self.paused:
            return
        self.t += dt
        if self.world.clock.tick(dt):
            self.world.new_day()
            if self.world.place_state.get("bakery", "").startswith(("burnt", "smoke")):
                self.world.place_state.pop("bakery", None)
                self.world.log("the bakery has been repaired", kind="event")
        for n in self.npcs:
            if not n.controlled:
                asleep = n.action == "go_home_rest" and n.at_place == n.home and n.busy_until > self.t
                n.tick_needs(dt / self.world.clock.seconds_per_game_hour * 6, asleep)  # rates are per game-hour/6
            self._move(n, dt)
            self._progress(n)
            if n.bubble and self.t >= n.bubble_until:
                n.bubble = None
        self._show_bubbles()
        self._tick_chat()
        self._perceive()
        self._tick_events()
        self._tick_incidents()
        self._tick_commitments()
        self._schedule_decisions()

    # --------------------------------------------------------------- perception

    def _perceive(self) -> None:
        """Each villager sees who is within VISION tiles (or in the same place); nothing else."""
        hhmm = self.world.clock.hhmm()
        for n in self.npcs:
            n.sees = set()
            for o in self.npcs:
                if o is n:
                    continue
                same_place = n.at_place is not None and n.at_place == o.at_place
                near = abs(n.x - o.x) + abs(n.y - o.y) <= VISION
                hidden = o.forced == "hiding somewhere" and not (abs(n.x - o.x) + abs(n.y - o.y) <= 3)
                if (same_place or near) and not hidden:
                    n.sees.add(o.id)
                    n.last_seen[o.id] = (o.where_word(self.world), hhmm, self.t)

    def force(self, n: NPC, condition: str, at: tuple[int, int], seconds: float) -> None:
        """An incident takes over a villager (hiding, ill in bed)."""
        self._finish(n)
        n.forced = condition
        n.action = "forced"
        n.current = Action("forced", condition, lambda a, w: "", lambda a, w: False, lambda a, w: None, lambda a, w, s: None, lambda a, w, s: None)
        n.action_label = condition
        n.target = at
        n.path = self.world.path(n.tile, at)
        n.busy_until = self.t + seconds
        n.remember(condition, significant=True)

    def unforce(self, n: NPC) -> None:
        if n.forced:
            n.forced = None
            self._finish(n)

    # ---------------------------------------------------------------- incidents

    def _tick_incidents(self) -> None:
        for inc in self.incidents:
            if inc.open and self.t >= inc.deadline_t:
                inc_mod.fail(inc, self)
        for n in self.villagers:
            if n.forced and n.busy_until and self.t >= n.busy_until and n.action == "forced":
                self.unforce(n)

    def publish_news(self, text: str, witnesses: list[NPC], subject: str | None = None) -> None:
        if any(item["text"] == text for item in self.world.news):
            return
        item = {"text": text, "since": self.world.clock.hhmm(), "knows": []}
        if subject:
            item["subject"] = subject
        self.world.news.append(item)
        del self.world.news[:-10]
        for w in witnesses:
            if not w.controlled:
                self._learn(w, text, source=None)

    def _tick_commitments(self) -> None:
        for n in self.villagers:
            for c in list(n.commitments):
                if self.t >= c["deadline_t"]:
                    n.commitments.remove(c)
                    to = self.by_id[c["to"]]
                    n.remember(f"never got around to my promise to {to.name}: {c['what']}", significant=True)
                    to.remember(f"{n.name} promised to {c['what']} and didn't", significant=True)
                    to.affinity[n.id] = max(-1.0, to.affinity.get(n.id, 0.0) - 0.4)
                    to.mood = max(0.0, to.mood - 0.4)
                    self.world.log(f"{n.name} broke a promise to {to.name}: {c['what']}")
                    self.publish_news(f"{n.name} doesn't keep their word", witnesses=[to], subject=n.id)

    # ------------------------------------------------------------------- player

    # --------------------------------------------------------------- reflection

    def maybe_reflect(self, n: NPC) -> None:
        """Called when a villager settles in for the night. Once per villager per day."""
        key = (n.id, self.world.day)
        if key in self._reflected or self.brain.name != "jev":
            return
        self._reflected.add(key)
        asyncio.create_task(self._reflect(n, self.world.day))

    async def _reflect(self, n: NPC, day: int) -> None:
        try:
            res = await reflection.reflect(self.judge_client(), n, day)
        except Exception as e:
            self.world.log(f"reflection error for {n.name}: {type(e).__name__}")
            return
        self.brain.usage.requests += 1
        if res is None:
            return
        change, p, conf = res
        if change == "unchanged" or conf < reflection.CONFIDENCE_TO_CHANGE:
            self.world.log(f"{n.name} sleeps; an ordinary day ({change} p={p:.2f}, conf {conf:.2f})", kind="reflection")
            return
        added = reflection.apply(n, change, p, day)
        self.world.log(f"{n.name} has become {change.replace('more_', 'more ')} (now: {', '.join(n.traits)})  p={p:.2f}", kind="change")

    def add_player(self) -> NPC:
        if self.player is None:
            x, y = self.world.places["market"].anchor
            self.player = traveler(float(x), float(y + 1))
            self.player.at_place = "market"
            self.player.action_label = "looking around"
            self.npcs.append(self.player)
            self.by_id[self.player.id] = self.player
            self.world.log("a traveler arrives at the square")
        return self.player

    def move_player(self, x: int, y: int) -> None:
        p = self.player or self.add_player()
        if p.talking_to:
            return  # finish the conversation first
        p.path = self.world.path(p.tile, (x, y))
        p.action_label = "walking" if p.path else "looking around"

    def player_wave(self) -> None:
        p = self.player or self.add_player()
        p.bubble, p.bubble_until = "👋", self.t + 2.0
        near = [n for n in self.npcs if not n.controlled and abs(n.x - p.x) + abs(n.y - p.y) <= 7]
        for n in near:
            n.remember("the traveler waved at me")
        p.remember("waved at " + (", ".join(n.name for n in near) if near else "nobody in particular"))
        self.world.log("the traveler waves" + (f" at {', '.join(n.name for n in near)}" if near else ""))

    # --------------------------------------------------------------- player chat

    def player_say(self, to_id: str, text: str) -> None:
        """The traveler speaks to a villager. Walks over first if needed; the villager waits."""
        p = self.player or self.add_player()
        n = self.by_id.get(to_id)
        if n is None or n.controlled or not text.strip():
            return
        if self.chat_with and self.chat_with != to_id:
            self.end_chat()
        self.chat_with = to_id
        self.chat_queue.append(text.strip()[:300])
        self.chat_last = self.t
        if abs(p.x - n.x) + abs(p.y - n.y) > CHAT_REACH:
            p.path = self.world.path(p.tile, n.tile)
            p.action_label = f"walking over to {n.name}"
            self.chat_status = "walking"
        self._tick_chat()

    def end_chat(self) -> None:
        n = self.by_id.get(self.chat_with or "")
        if n is not None:
            if n.talking_to == "traveler":
                n.remember("talked with the traveler")
                self._finish(n)
            n.busy_until = 0.0
        if self.player and self.player.talking_to:
            self.player.talking_to = None
            self.player.action = None
            self.player.action_label = "looking around"
        self.chat_with, self.chat_queue, self.chat_status = None, [], ""

    def _tick_chat(self) -> None:
        if not self.chat_with:
            return
        n, p = self.by_id[self.chat_with], self.player
        if self.t - self.chat_last > CHAT_IDLE_SECONDS and not self.chat_queue:
            self.end_chat()
            return
        near = abs(p.x - n.x) + abs(p.y - n.y) <= CHAT_REACH
        if not near:
            if not p.path:
                p.path = self.world.path(p.tile, n.tile)
            return
        if n.talking_to not in (None, "traveler"):
            self.chat_status = f"{n.name} is talking with {self.by_id[n.talking_to].name}"
            return
        if n.talking_to != "traveler":
            # hold the villager in place for the chat
            if n.action and n.action != "go_home_rest" and not n.forced:
                n.path = []
            n.talking_to = "traveler"
            n.action_label = "talking with the traveler"
            p.talking_to = n.id
            p.action = "conversation"
            p.action_label = f"talking with {n.name}"
            p.path = []
        n.busy_until = max(n.busy_until, self.t + CHAT_IDLE_SECONDS)
        if self.chat_queue and self.chat_status != "thinking":
            text = self.chat_queue.pop(0)
            self.chat_status = "thinking"
            asyncio.create_task(self._player_exchange(n, text))
        elif self.chat_status != "thinking":
            self.chat_status = "talking"

    async def _player_exchange(self, n: NPC, text: str) -> None:
        p = self.player
        n.chat.append(("Traveler", text))
        del n.chat[:-10]
        p.bubble, p.bubble_until = text, self.t + max(3.0, min(8.0, len(text) / 12))
        self.world.log(f'  Traveler → {n.name}: "{text}"')
        try:
            reply, proposed = await self.narrator.reply(n, text, self.world)
        except Exception as e:
            self.world.log(f"reply error: {type(e).__name__}")
            reply, proposed = "...", {}
        n.chat.append((n.name, reply))
        n.bubble, n.bubble_until = reply, self.t + max(3.5, min(9.0, len(reply) / 11))
        self.world.log(f'  {n.name}: "{reply}"')
        self.chat_status = "talking"
        self.chat_last = self.t
        try:
            out = await judge_traveler(self.judge_client(), n, text, reply, proposed, self.world)
        except Exception as e:
            self.world.log(f"traveler judge error: {type(e).__name__}")
            return
        self.brain.usage.requests += 1
        # --- what your words did, decided by Jev, applied by code ----------------------
        delta = {0: -0.25, 1: 0.05, 2: 0.2}[int(round(min(2, max(0, out.tone))))]
        if out.hostile >= 0.6:
            delta -= 0.3
            n.remember("the traveler was rude to me", significant=True)
        n.affinity["traveler"] = max(-1.0, min(1.0, n.affinity.get("traveler", 0.0) + delta))
        if out.tone <= 0.5:
            n.mood = max(0.0, n.mood - 0.2)
        elif out.tone >= 1.5:
            n.mood = min(2.0, n.mood + 0.2)
        notes = [f"tone {out.tone:.1f}"]
        if proposed.get("news"):
            claim = str(proposed["news"])
            if out.claim_present >= 0.6 and out.believed >= 0.6:
                subject = next((v.id for v in self.villagers if v.name.lower() in claim.lower()), None)
                self.publish_news(claim, witnesses=[n], subject=subject)
                n.remember(f"the traveler told me that {claim}", significant=True)
                notes.append(f"believed (p={out.believed:.2f})")
            elif out.claim_present >= 0.6:
                n.remember(f"the traveler claimed that {claim}; I'm not sure I believe it")
                notes.append(f"not believed (p={out.believed:.2f})")
        if proposed.get("request"):
            req = str(proposed["request"])
            if out.request_present >= 0.6 and out.agreed >= 0.6:
                self._n_commit += 1
                n.commitments.append({"id": f"c{self._n_commit}", "to": "traveler", "what": req, "deadline_t": self.t + PROMISE_SECONDS})
                n.remember(f"agreed to the traveler's request: {req}", significant=True)
                notes.append(f"agreed (p={out.agreed:.2f})")
            elif out.request_present >= 0.6:
                n.remember(f"the traveler asked me to {req}; I didn't commit")
                notes.append(f"declined (p={out.agreed:.2f})")
        self.world.log(f"{n.name} on the traveler: {'; '.join(notes)}", kind="event")

    # ------------------------------------------------------------------- events

    def _tick_events(self) -> None:
        if self.world.weather == "raining" and self.t >= self._rain_until:
            self.world.weather = "clear"
            self.world.log("the rain stops")
        if self.t < self._next_event:
            return
        self._next_event = self.t + self.world.rng.uniform(*EVENT_EVERY)
        if self.world.rng.random() < INCIDENT_CHANCE:
            kind = inc_mod.pick_kind(self.world.rng, self)
            if kind:
                self._n_inc += 1
                inc = inc_mod.spawn(kind, self.world, self.t, self._n_inc)
                self.incidents.append(inc)
                del self.incidents[:-6]
                witnesses = inc_mod.on_spawn(inc, self)
                self.publish_news(inc.text, witnesses=witnesses, subject=inc.victim)
                for w in witnesses:
                    w.remember(f"saw that {inc.text}", significant=True)
                self.world.log(f"incident: {inc.text} (seen by {', '.join(w.name for w in witnesses)})")
                return
        key, text = self.world.rng.choice(EVENTS)
        if key == "rain":
            if self.world.weather == "raining":
                return
            self.world.weather = "raining"
            self._rain_until = self.t + RAIN_SECONDS
            self.world.log("it starts to rain")
            for n in self.npcs:
                if not n.controlled:
                    n.remember("it started raining")
            return
        if any(item["text"] == text for item in self.world.news):
            return
        where = EVENT_WITNESS.get(key)
        witnesses = [n for n in self.villagers if n.at_place == where or (where and self.world.place_at(*n.tile) == where)]
        if not witnesses:
            witnesses = [self.world.rng.choice(self.villagers)]
        self.publish_news(text, witnesses)
        self.world.log(f"news: {text} (seen by {', '.join(n.name for n in witnesses)})")

    def _learn(self, n: NPC, text: str, source: NPC | None) -> None:
        if text in n.knows:
            return
        item = next((i for i in self.world.news if i["text"] == text), None)
        about_me = item is not None and item.get("subject") == n.id and source is not None
        n.knows.append(text)
        del n.knows[:-5]
        if about_me:
            n.remember(f"{source.name} told me people are saying: {text}", significant=True)
            n.mood = max(0.0, n.mood - 0.6)
            n.affinity[source.id] = max(-1.0, n.affinity.get(source.id, 0.0) - 0.2)
            self.world.log(f"{n.name} hears the rumor about themselves from {source.name}")
        else:
            n.remember(f"heard from {source.name} that {text}" if source else f"saw that {text}")
        if item is not None and n.id not in item["knows"]:
            item["knows"].append(n.id)

    def _move(self, n: NPC, dt: float) -> None:
        if not n.path:
            return
        tx, ty = n.path[0]
        dx, dy = tx - n.x, ty - n.y
        step = n.speed * dt
        dist = abs(dx) + abs(dy)
        if dist <= step:
            n.x, n.y = float(tx), float(ty)
            n.path.pop(0)
            n.at_place = self.world.place_at(tx, ty)
        else:
            n.x += step * (dx / dist)
            n.y += step * (dy / dist)
            n.at_place = None

    def _progress(self, n: NPC) -> None:
        if n.controlled and n.action != "conversation":
            if not n.path and n.action_label == "walking":
                n.action_label = "looking around"
                n.at_place = self.world.place_at(*n.tile)
            return
        if n.action is None:
            return
        if n.talking_to and n.action.startswith("talk_to_"):
            self._progress_talk_intent(n)
            return
        act = n.current
        if act is None:
            return
        if n.path:
            return
        if n.busy_until == 0.0:  # just arrived
            n.at_place = self.world.place_at(*n.tile)
            act.on_arrive(n, self.world, self)
            if n.busy_until == 0.0:
                n.busy_until = self.t + 0.1
            return
        if self.t >= n.busy_until:
            act.on_complete(n, self.world, self)
            self._finish(n)

    def _progress_talk_intent(self, n: NPC) -> None:
        other = self.by_id[n.talking_to]
        if other.talking_to not in (None, n.id):  # they got taken; give up
            n.remember(f"went to find {other.name} but they were busy")
            self._finish(n)
            return
        if abs(n.x - other.x) + abs(n.y - other.y) <= 1.6:
            self._start_conversation(n, other)
            return
        if not n.path:
            if other.id in n.sees:
                # they moved but are in view; follow, but not forever
                n.target = other.tile
                n.path = self.world.path(n.tile, other.tile)
                n._chase += 1
                if not n.path or n._chase > 6:
                    n.remember(f"couldn't catch up with {other.name}")
                    self._finish(n)
            else:
                # arrived where they believed the other was; they're not there
                where = n.last_seen.get(other.id, ("somewhere", "", 0))[0]
                at = where if where.startswith("on ") else f"at {where}"
                n.remember(f"went looking for {other.name} {at} but they weren't there")
                self.world.log(f"{n.name} looked for {other.name} at {where}; not there")
                self._finish(n)

    def _finish(self, n: NPC) -> None:
        n.action = None
        n.current = None
        n.action_label = "standing around"
        n.target = None
        n.path = []
        n.busy_until = 0.0
        n.talking_to = None
        n._chase = 0

    # ------------------------------------------------------------ conversations

    def _start_conversation(self, a: NPC, b: NPC) -> None:
        if b.action and b.action != "go_home_rest":
            b.path = []  # interrupt whatever they were walking to
        a.talking_to, b.talking_to = b.id, a.id
        a.action = b.action = "conversation"
        a.action_label, b.action_label = f"talking with {b.name}", f"talking with {a.name}"
        a.busy_until = b.busy_until = self.t + CONVERSATION_SECONDS
        a.path = b.path = []
        topic = a.topic or b.topic or "the_weather"
        # News spreads here: if one side knows something the other doesn't and Jev said
        # they'd pass it on, that becomes the topic and the listener learns it.
        for speaker, listener in ((a, b), (b, a)):
            fresh = [t for t in speaker.knows if t not in listener.knows]
            if fresh and speaker.share_news >= SHARE_NEWS and not speaker.controlled:
                topic = f"news:{fresh[-1]}"
                if not listener.controlled:
                    self._learn(listener, fresh[-1], source=speaker)
                    self.world.log(f"{speaker.name} tells {listener.name}: {fresh[-1]}")
                break
        a.topic = b.topic = topic  # both remember the same conversation
        if not topic.startswith("news:"):
            self.world.log(f"{a.name} and {b.name} talk about {topic.replace('_', ' ')}")
        asyncio.create_task(self._narrate(a, b, topic))
        conv = Action("conversation", "talking", lambda n, w: "", lambda n, w: False, lambda n, w: None, lambda n, w, s: None, self._end_conversation)
        a.current = b.current = conv

    async def _narrate(self, a: NPC, b: NPC, topic: str) -> None:
        proposed = Proposed()
        try:
            lines, proposed = await self.narrator.converse(a, b, topic, self.world)
        except Exception as e:  # a narrator failure must never break the sim
            self.world.log(f"narrator error: {type(e).__name__}")
            lines = []
        if lines and not (a.controlled or b.controlled):
            asyncio.create_task(self._judge_outcome(a, b, lines, proposed))
        for who, text in lines[:4]:
            self.world.log(f'  {who}: "{text}"')
        at = self.t + 0.5
        for who, text in lines[:4]:
            self._bubbles.append((at, a if who == a.name else b, text))
            at += LINE_SECONDS
        # a slow narrator must not have its lines outlive the conversation
        for n in (a, b):
            if n.talking_to:
                n.busy_until = max(n.busy_until, at + 0.5)

    def _show_bubbles(self) -> None:
        keep = []
        for at, npc, text in self._bubbles:
            if self.t >= at:
                npc.bubble = text
                npc.bubble_until = self.t + LINE_SECONDS - 0.3
            else:
                keep.append((at, npc, text))
        self._bubbles = keep

    async def _judge_outcome(self, a: NPC, b: NPC, lines: list[tuple[str, str]], proposed: Proposed) -> None:
        """Jev reads the lines: how did it go, and is the LLM's proposed promise/rumor really in there?"""
        between = [{"id": c["id"], "who": x.id, "to": c["to"], "what": c["what"]} for x, y in ((a, b), (b, a)) for c in x.commitments if c["to"] == y.id]
        try:
            out: Outcome = await judge(self.judge_client(), a, b, lines, proposed, {n.name: n for n in self.villagers}, between)
        except Exception as e:
            self.world.log(f"outcome judge error: {type(e).__name__}")
            return
        self.brain.usage.requests += 1
        delta = {0: -0.3, 1: 0.0, 2: 0.2}[int(round(min(2, max(0, out.tone))))]
        if delta:
            for x, y in ((a, b), (b, a)):
                x.affinity[y.id] = max(-1.0, min(1.0, x.affinity.get(y.id, 0.0) + delta))
            if delta < 0:
                a.remember(f"had a tense exchange with {b.name}", significant=True)
                b.remember(f"had a tense exchange with {a.name}", significant=True)
                self.world.log(f"{a.name} and {b.name} part on bad terms (tone {out.tone:.1f})")
        for cid in out.fulfilled or []:
            for x, y in ((a, b), (b, a)):
                c = next((c for c in x.commitments if c["id"] == cid), None)
                if c:
                    x.commitments.remove(c)
                    x.remember(f"kept my promise to {y.name}: {c['what']}", significant=True)
                    y.remember(f"{x.name} kept their promise: {c['what']}", significant=True)
                    y.affinity[x.id] = min(1.0, y.affinity.get(x.id, 0.0) + 0.35)
                    self.world.log(f"{x.name} keeps a promise to {y.name} in conversation: {c['what']}")
        if out.promise and not out.fulfilled:  # a promise kept in this very exchange is not a new promise
            who = self.by_id[out.promise[0]]
            to = b if who is a else a
            existing = next((c for c in who.commitments if c["to"] == to.id), None)
            if existing:
                existing["deadline_t"] = self.t + PROMISE_SECONDS  # a repeat is a renewal, not a second promise
                self.world.log(f"{who.name} renews the promise to {to.name}: {existing['what']}")
            else:
                self._n_commit += 1
                who.commitments.append({"id": f"c{self._n_commit}", "to": to.id, "what": out.promise[1], "deadline_t": self.t + PROMISE_SECONDS})
                who.remember(f"promised {to.name} to {out.promise[1]}")
                to.remember(f"{who.name} promised to {out.promise[1]}")
                self.world.log(f"{who.name} promises {to.name}: {out.promise[1]}  (Jev p={out.p_promise:.2f})")
        elif proposed.promise_what:
            self.world.log(f"narrator claimed a promise by {proposed.promise_by}; Jev didn't find it in the lines (p={out.p_promise:.2f})")
        if out.rumor:
            subject = self.by_id[out.rumor[0]]
            self.publish_news(out.rumor[1], witnesses=[a, b], subject=subject.id)
            self.world.log(f"rumor about {subject.name}: {out.rumor[1]}  (Jev p={out.p_rumor:.2f})")
        elif proposed.rumor_what:
            self.world.log(f"narrator claimed a rumor about {proposed.rumor_about}; Jev didn't confirm it (p={out.p_rumor:.2f})")

    def _end_conversation(self, n: NPC, world: World, sim) -> None:
        other = self.by_id.get(n.talking_to or "")
        n.needs["social"] = max(0.0, n.needs["social"] - 0.6)
        topic = (n.topic or "things")
        topic = "the news" if topic.startswith("news:") else topic.replace("_", " ")
        if other:
            if not n.controlled:
                n.remember(f"talked with {other.name} about {topic}")
                n.affinity[other.id] = min(1.0, n.affinity.get(other.id, 0.0) + AFFINITY_PER_CHAT)
            n.mood = min(2.0, n.mood + 0.3)

    # ---------------------------------------------------------------- decisions

    def _schedule_decisions(self) -> None:
        if self._inflight and not self._inflight.done():
            return
        if time.monotonic() - self._last_request < MIN_REQUEST_GAP:
            return
        idle = [n for n in self.npcs if n.idle and not n.deciding and not n.controlled and not n.forced]
        if not idle:
            return
        for n in idle:
            n.deciding = True
        self._last_request = time.monotonic()
        self._inflight = asyncio.create_task(self._decide(idle))

    async def _decide(self, npcs: list[NPC]) -> None:
        options = {n.id: self.options_for(n) for n in npcs}
        known = {n.id: [i.text for i in self.incidents if i.open and i.text in n.knows] for n in npcs}
        try:
            decisions = await self.brain.decide(self.world, npcs, options, known)
        except Exception as e:  # fail safe: a wandering villager beats a frozen one
            self.brain.usage.errors += 1
            self.world.log(f"brain error ({type(e).__name__}); using random for this round")
            decisions = await self.fallback.decide(self.world, npcs, options)
        for n in npcs:
            n.deciding = False
            d = decisions.get(n.id)
            if d is None:
                continue
            self._apply(n, d, options[n.id])

    def _apply(self, n: NPC, d: Decision, acts: list[Action]) -> None:
        act = next((a for a in acts if a.id == d.action_id), None) or acts[0]
        n.probs, n.confidence, n.mood = d.probs, d.confidence, d.mood
        n.share_news = d.share_news
        n.help_p = d.help
        n.last_options = {a.id: a.describe(n, self.world) for a in acts}
        if d.topic:
            n.topic = d.topic
        n.action = act.id
        n.current = act
        n.action_label = act.label
        n.busy_until = 0.0
        act.start(n, self.world)
        if n.target and n.target != n.tile:
            n.path = self.world.path(n.tile, n.target)
            if not n.path:
                self._finish(n)
        top = f"{d.action_id} ({d.probs.get(d.action_id, 0):.2f})"
        self.world.log(f"{n.name} → {top}")

    # ----------------------------------------------------------------- snapshot

    def snapshot(self) -> dict:
        from jevlab.cost import USD_PER_MTOK

        nar = {"name": self.narrator.name, "calls": self.narrator.calls}
        if hasattr(self.narrator, "input_tokens"):
            model = getattr(self.narrator, "model", "")
            nar["name"] = f"{self.narrator.name} ({model})"
            in_p, out_p = (1.0, 5.0) if "haiku" in model else (2.0, 10.0) if "sonnet" in model else (5.0, 25.0)  # $/Mtok
            nar["cost_usd"] = round(self.narrator.input_tokens / 1e6 * in_p + self.narrator.output_tokens / 1e6 * out_p, 4)
        return {
            "t": round(self.t, 1),
            "clock": {"hhmm": self.world.clock.hhmm(), "label": self.world.clock.label(), "daylight": round(self.world.clock.daylight, 2)},
            "speed": self.speed,
            "paused": self.paused,
            "npcs": [n.snapshot() for n in self.npcs],
            "weather": self.world.weather,
            "news": self.world.news[-8:],
            "day": self.world.day,
            "chat": {"with": self.chat_with, "status": self.chat_status, "log": self.by_id[self.chat_with].chat[-10:] if self.chat_with else []},
            "chronicle": self.world.chronicle[-120:],
            "incidents": [
                {"id": i.id, "kind": i.kind, "text": i.text, "job": i.job, "needed": i.needed, "progress": i.progress,
                 "helpers": i.helpers, "resolved": i.resolved, "failed": i.failed, "left": max(0, round(i.deadline_t - self.t))}
                for i in self.incidents[-4:]
            ],
            "events": self.world.events[-14:],
            "brain": {"name": self.brain.name, **self.brain.usage.snapshot(USD_PER_MTOK)},
            "narrator": nar,
        }
