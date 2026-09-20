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

from typesafe_sdk import AsyncTypeSafeClient, Choice, Noul, Score

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
    share_news: float = 0.0
    help: float = 0.0


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

    async def decide(self, world: World, npcs: list[NPC], options: dict[str, list[Action]], incidents_known: dict | None = None) -> dict[str, Decision]: ...


class RandomBrain:
    name = "random"

    def __init__(self, seed: int = 1) -> None:
        self.rng = random.Random(seed)
        self.usage = Usage()

    async def decide(self, world, npcs, options, incidents_known=None):
        out = {}
        for n in npcs:
            acts = options[n.id]
            pick = self.rng.choice(acts)
            out[n.id] = Decision(pick.id, {a.id: (0.6 if a is pick else 0.4 / max(1, len(acts) - 1)) for a in acts}, 0.2, 1.0, self.rng.choice(list(TOPICS)), self.rng.random())
        return out


class JevBrain:
    name = "jev"

    def __init__(self, model: str | None = None) -> None:
        self.client = AsyncTypeSafeClient(model=model)
        self.usage = Usage()

    def _state(self, world: World, n: NPC) -> dict:
        """The village as *this* villager knows it: themselves in full, others as seen or remembered."""
        st = {
            "time": f"{world.clock.hhmm()}, {world.clock.label()}",
            "weather": world.weather,
            "me": n.describe(world),
            "others": {o.name: n.describe_other(o, world) for o in world.npcs if o is not n and not o.controlled},
            "places": {p.name: p.describe(world, viewer=n) for p in world.places.values() if p.kind != "home"},
        }
        trav = next((o for o in world.npcs if o.controlled), None)
        if trav and trav.id in n.sees:
            st["traveler"] = {"description": "a stranger passing through, new to the village", "at": trav.where_word(world), "doing": trav.action_label}
        return st

    def _questions(self, world: World, n: NPC, acts: list[Action], incidents_known: list[str]) -> dict:
        qs: dict = {
            "act": Choice(
                instructions=(
                    f"It is {world.clock.label()}. `me` is {n.name}, a {n.role}. Given how they feel, their traits, "
                    f"what they remember and have promised, and what they know about where others are, what do they do next?"
                ),
                criteria={a.id: a.describe(n, world) for a in acts},
            ),
            "mood": Score(
                instructions=f"What mood is `me` ({n.name}) in right now, judging by how they feel and what happened recently?",
                criteria=[
                    "Gloomy or irritable: tired, hungry, lonely, or something recently went badly",
                    "Neutral: getting on with the day",
                    "Cheerful: rested, fed, in good company, or something recently went well",
                ],
            ),
        }
        if n.knows:
            qs["share"] = Noul(
                instructions=f"If `me` ({n.name}) chats with someone now, they would pass on the news they have heard (`me.has_heard`)",
                criteria={"true": "Their traits and mood make them likely to bring up what they heard", "false": "They keep it to themselves, or are too tired or preoccupied to bother"},
            )
        if incidents_known:
            qs["help"] = Noul(
                instructions=f"`me` ({n.name}) would drop what they are doing right now to help with this: {incidents_known[-1]}",
                criteria={"true": "Their traits, mood, and relationships make them the kind to step up, and they are able to", "false": "They would leave it to others, are too tired or self-absorbed, or don't care for the people involved"},
            )
        if any(a.id.startswith("talk_to_") for a in acts):
            qs["topic"] = Choice(
                instructions=f"If `me` ({n.name}) talks to someone in the next while, what would they most likely bring up?",
                criteria=dict(TOPICS),
            )
        return qs

    async def _decide_one(self, world: World, n: NPC, acts: list[Action], incidents_known: list[str]) -> Decision:
        resp = await self.client.system_one(state=self._state(world, n), questions=self._questions(world, n, acts, incidents_known), timeout=10.0)
        self.usage.requests += 1
        self.usage.input_tokens += resp.usage.input_tokens
        act = resp.choices["act"]
        d = Decision(act.choice, dict(act.probabilities), act.confidence, resp.scores["mood"].score)
        d.topic = resp.choices["topic"].choice if "topic" in resp.choices else None
        d.share_news = resp.nouls["share"].noul if "share" in resp.nouls else 0.0
        d.help = resp.nouls["help"].noul if "help" in resp.nouls else 0.0
        return d

    async def decide(self, world, npcs, options, incidents_known=None):
        """One request per villager (their views differ), all in flight at once."""
        import asyncio

        t0 = time.perf_counter()
        incidents_known = incidents_known or {}
        results = await asyncio.gather(*(self._decide_one(world, n, options[n.id], incidents_known.get(n.id, [])) for n in npcs))
        self.usage.seconds += time.perf_counter() - t0
        return {n.id: d for n, d in zip(npcs, results)}

    async def close(self) -> None:
        await self.client.__aexit__(None, None, None)
