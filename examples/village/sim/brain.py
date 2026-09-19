"""Brains decide what NPCs do next. JevBrain asks Jev; RandomBrain is the fallback.

The split that makes this work: the brain never sees coordinates, paths, or numbers.
It sees the village as words (who is where, how everyone feels, what they remember)
and returns one bounded action per NPC plus a couple of cheap speculative judgments
(mood, what they'd talk about) from the same request.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from typing import Protocol

from typesafe_sdk import AsyncTypeSafeClient, Choice, Score

from .actions import Action
from .npc import NPC
from .world import World

TOPICS = {
    "the_harvest": "how the harvest is going",
    "the_weather": "the weather",
    "gossip": "gossip about someone else in the village",
    "the_well": "the state of the well",
    "trade": "prices, trade, and money",
    "old_times": "stories about the old days",
    "the_kid": "what Pip has been up to",
    "food": "food and drink",
}


@dataclass
class Decision:
    action_id: str
    probs: dict[str, float]
    confidence: float
    mood: float = 1.0
    topic: str | None = None


@dataclass
class Usage:
    requests: int = 0
    input_tokens: int = 0
    seconds: float = 0.0
    errors: int = 0

    def snapshot(self, usd_per_mtok: float) -> dict:
        return {"requests": self.requests, "input_tokens": self.input_tokens, "cost_usd": round(self.input_tokens / 1e6 * usd_per_mtok, 4), "errors": self.errors}


class Brain(Protocol):
    name: str
    usage: Usage

    async def decide(self, world: World, npcs: list[NPC], options: dict[str, list[Action]]) -> dict[str, Decision]: ...


class RandomBrain:
    name = "random"

    def __init__(self, seed: int = 1) -> None:
        self.rng = random.Random(seed)
        self.usage = Usage()

    async def decide(self, world, npcs, options):
        out = {}
        for n in npcs:
            acts = options[n.id]
            pick = self.rng.choice(acts)
            out[n.id] = Decision(pick.id, {a.id: (0.6 if a is pick else 0.4 / max(1, len(acts) - 1)) for a in acts}, 0.2, 1.0, self.rng.choice(list(TOPICS)))
        return out


class JevBrain:
    name = "jev"

    def __init__(self, model: str | None = None) -> None:
        self.client = AsyncTypeSafeClient(model=model)
        self.usage = Usage()

    def _state(self, world: World, deciding: list[NPC]) -> dict:
        return {
            "time": f"{world.clock.hhmm()}, {world.clock.label()}",
            "villagers": {n.id: n.describe(world) for n in world.npcs},
            "places": {p.name: p.describe(world) for p in world.places.values() if p.kind != "home"},
        }

    def _questions(self, world: World, deciding: list[NPC], options: dict[str, list[Action]]) -> dict:
        qs: dict = {}
        for n in deciding:
            acts = options[n.id]
            qs[f"act|{n.id}"] = Choice(
                instructions=(
                    f"It is {world.clock.label()}. Given how `villagers.{n.id}` feels, their traits, what they "
                    f"remember, and where everyone is, what does {n.name} do next?"
                ),
                criteria={a.id: a.describe(n, world) for a in acts},
            )
            qs[f"mood|{n.id}"] = Score(
                instructions=f"What mood is `villagers.{n.id}` in right now, judging by how they feel and what happened recently?",
                criteria=[
                    "Gloomy or irritable: tired, hungry, lonely, or something recently went badly",
                    "Neutral: getting on with the day",
                    "Cheerful: rested, fed, in good company, or something recently went well",
                ],
            )
            if any(a.id.startswith("talk_to_") for a in acts):
                # Speculative: only used if they end up in a conversation. Same request, no extra latency.
                qs[f"topic|{n.id}"] = Choice(
                    instructions=f"If `villagers.{n.id}` talks to someone in the next while, what would they most likely bring up?",
                    criteria=dict(TOPICS),
                )
        return qs

    async def decide(self, world, npcs, options):
        t0 = time.perf_counter()
        resp = await self.client.system_one(state=self._state(world, npcs), questions=self._questions(world, npcs, options), timeout=10.0)
        self.usage.requests += 1
        self.usage.input_tokens += resp.usage.input_tokens
        self.usage.seconds += time.perf_counter() - t0
        out: dict[str, Decision] = {}
        for n in npcs:
            act = resp.choices[f"act|{n.id}"]
            mood = resp.scores[f"mood|{n.id}"].score
            topic = resp.choices[f"topic|{n.id}"].choice if f"topic|{n.id}" in resp.choices else None
            out[n.id] = Decision(act.choice, dict(act.probabilities), act.confidence, mood, topic)
        return out

    async def close(self) -> None:
        await self.client.__aexit__(None, None, None)
