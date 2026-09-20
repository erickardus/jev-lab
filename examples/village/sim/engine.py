"""The simulation loop: movement, needs, arrivals, conversations, and asking the brain.

Runs on a fixed step. Decisions are asynchronous: when villagers become idle they are
batched into one brain request per tick, and the world keeps moving while it is in
flight. Nothing here blocks on a model.
"""

from __future__ import annotations

import asyncio
import time

from .actions import Action, build_actions, talk_action
from .brain import Brain, Decision, RandomBrain
from .narrator import Narrator, TemplateNarrator
from .npc import NPC, default_cast, traveler
from .world import World

CONVERSATION_SECONDS = 14.0
LINE_SECONDS = 3.5
MIN_REQUEST_GAP = 0.6  # real seconds between brain requests, so a burst of idle NPCs is one request
EVENT_EVERY = (45.0, 90.0)  # sim seconds between village events (random in range)
RAIN_SECONDS = 60.0
SHARE_NEWS = 0.5  # p(would pass on news) above which a conversation spreads it
AFFINITY_PER_CHAT = 0.2

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

    # ------------------------------------------------------------------ options

    def options_for(self, npc: NPC) -> list[Action]:
        acts = [a for a in self.actions.values() if a.available(npc, self.world)]
        for other in self.npcs:
            if other is not npc:
                ta = talk_action(other)
                if ta.available(npc, self.world):
                    acts.append(ta)
        return acts

    # --------------------------------------------------------------------- tick

    def tick(self, dt: float) -> None:
        if self.paused:
            return
        self.t += dt
        self.world.clock.tick(dt)
        for n in self.npcs:
            if not n.controlled:
                asleep = n.action == "go_home_rest" and n.at_place == n.home and n.busy_until > self.t
                n.tick_needs(dt / self.world.clock.seconds_per_game_hour * 6, asleep)  # rates are per game-hour/6
            self._move(n, dt)
            self._progress(n)
            if n.bubble and self.t >= n.bubble_until:
                n.bubble = None
        self._show_bubbles()
        self._tick_events()
        self._schedule_decisions()

    # ------------------------------------------------------------------- player

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

    # ------------------------------------------------------------------- events

    def _tick_events(self) -> None:
        if self.world.weather == "raining" and self.t >= self._rain_until:
            self.world.weather = "clear"
            self.world.log("the rain stops")
        if self.t < self._next_event:
            return
        self._next_event = self.t + self.world.rng.uniform(*EVENT_EVERY)
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
        self.world.news.append({"text": text, "since": self.world.clock.hhmm(), "knows": []})
        where = EVENT_WITNESS.get(key)
        witnesses = [n for n in self.npcs if not n.controlled and (n.at_place == where or (where and self.world.place_at(*n.tile) == where))]
        if not witnesses:
            witnesses = [self.world.rng.choice([n for n in self.npcs if not n.controlled])]
        for n in witnesses:
            self._learn(n, text, source=None)
        self.world.log(f"news: {text} (seen by {', '.join(n.name for n in witnesses)})")

    def _learn(self, n: NPC, text: str, source: NPC | None) -> None:
        if text in n.knows:
            return
        n.knows.append(text)
        del n.knows[:-5]
        n.remember(f"heard from {source.name} that {text}" if source else f"saw that {text}")
        for item in self.world.news:
            if item["text"] == text and n.id not in item["knows"]:
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
            # they moved; follow, but not forever
            n.target = other.tile
            n.path = self.world.path(n.tile, other.tile)
            n._chase += 1
            if not n.path or n._chase > 6:
                n.remember(f"couldn't catch up with {other.name}")
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
        try:
            lines = await self.narrator.converse(a, b, topic, self.world)
        except Exception as e:  # a narrator failure must never break the sim
            self.world.log(f"narrator error: {type(e).__name__}")
            lines = []
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
        idle = [n for n in self.npcs if n.idle and not n.deciding and not n.controlled]
        if not idle:
            return
        for n in idle:
            n.deciding = True
        self._last_request = time.monotonic()
        self._inflight = asyncio.create_task(self._decide(idle))

    async def _decide(self, npcs: list[NPC]) -> None:
        options = {n.id: self.options_for(n) for n in npcs}
        try:
            decisions = await self.brain.decide(self.world, npcs, options)
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
            "news": self.world.news[-6:],
            "events": self.world.events[-14:],
            "brain": {"name": self.brain.name, **self.brain.usage.snapshot(USD_PER_MTOK)},
            "narrator": nar,
        }
