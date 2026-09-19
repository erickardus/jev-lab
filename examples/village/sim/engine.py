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
from .npc import NPC, default_cast
from .world import World

CONVERSATION_SECONDS = 14.0
LINE_SECONDS = 3.5
MIN_REQUEST_GAP = 0.6  # real seconds between brain requests, so a burst of idle NPCs is one request


class Sim:
    def __init__(self, brain: Brain, narrator: Narrator | None = None, seed: int = 7) -> None:
        self.world = World(seed)
        self.npcs: list[NPC] = default_cast()
        self.world.npcs = self.npcs
        self.by_id = {n.id: n for n in self.npcs}
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
            asleep = n.action == "go_home_rest" and n.at_place == n.home and n.busy_until > self.t
            n.tick_needs(dt / self.world.clock.seconds_per_game_hour * 6, asleep)  # rates are per game-hour/6
            self._move(n, dt)
            self._progress(n)
            if n.bubble and self.t >= n.bubble_until:
                n.bubble = None
        self._show_bubbles()
        self._schedule_decisions()

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
        if n.action is None:
            return
        if n.talking_to and n.action.startswith("talk_to_"):
            self._progress_talk_intent(n)
            return
        act = self.actions.get(n.action)
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
        a.topic = b.topic = topic  # both remember the same conversation
        self.world.log(f"{a.name} and {b.name} talk about {topic.replace('_', ' ')}")
        asyncio.create_task(self._narrate(a, b, topic))
        self.actions["conversation"] = Action(
            "conversation", "talking", lambda n, w: "", lambda n, w: False, lambda n, w: None, lambda n, w, s: None,
            self._end_conversation,
        )

    async def _narrate(self, a: NPC, b: NPC, topic: str) -> None:
        try:
            lines = await self.narrator.converse(a, b, topic, self.world)
        except Exception as e:  # a narrator failure must never break the sim
            self.world.log(f"narrator error: {type(e).__name__}")
            lines = []
        at = self.t + 0.5
        for who, text in lines[:4]:
            self._bubbles.append((at, a if who == a.name else b, text))
            at += LINE_SECONDS

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
        topic = (n.topic or "things").replace("_", " ")
        if other:
            n.remember(f"talked with {other.name} about {topic}")
            n.mood = min(2.0, n.mood + 0.3)

    # ---------------------------------------------------------------- decisions

    def _schedule_decisions(self) -> None:
        if self._inflight and not self._inflight.done():
            return
        if time.monotonic() - self._last_request < MIN_REQUEST_GAP:
            return
        idle = [n for n in self.npcs if n.idle and not n.deciding]
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
        if d.topic:
            n.topic = d.topic
        n.action = act.id
        n.action_label = act.label
        n.busy_until = 0.0
        act.start(n, self.world)
        if act.id.startswith("talk_to_"):
            self.actions[act.id] = act  # so _progress can find it
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
            nar["cost_usd"] = round(self.narrator.input_tokens / 1e6 * 5 + self.narrator.output_tokens / 1e6 * 25, 4)
        return {
            "t": round(self.t, 1),
            "clock": {"hhmm": self.world.clock.hhmm(), "label": self.world.clock.label(), "daylight": round(self.world.clock.daylight, 2)},
            "speed": self.speed,
            "paused": self.paused,
            "npcs": [n.snapshot() for n in self.npcs],
            "events": self.world.events[-12:],
            "brain": {"name": self.brain.name, **self.brain.usage.snapshot(USD_PER_MTOK)},
            "narrator": nar,
        }
