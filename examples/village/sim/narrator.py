"""Narrators turn a decided conversation into words. This is the LLM seam.

Jev decides *that* Mira talks to Bram and *what about* (fast, cheap, every few seconds,
for every villager). Writing the actual lines is generation, which Jev doesn't do, so it
goes to a narrator: canned templates by default, Claude when ANTHROPIC_API_KEY is set
and --narrator claude is passed. Conversations are rare (a few per minute across the
village) and can take a second, so a slow generative call here costs nothing visible.

The same seam is where other generative behaviors would plug in later: a villager
writing a notice for the square, a rumor mutating as it passes from mouth to mouth.
"""

from __future__ import annotations

import random
from typing import Protocol

from .brain import TOPICS
from .npc import NPC
from .world import World


class Narrator(Protocol):
    name: str
    calls: int

    async def converse(self, a: NPC, b: NPC, topic: str, world: World) -> list[tuple[str, str]]: ...


class TemplateNarrator:
    name = "template"

    LINES = {
        "the_harvest": ["How's the wheat coming along?", "Slow. Needs rain.", "Bram says another week."],
        "the_weather": ["Cold morning.", "Warmer by noon, they say.", "My knees disagree."],
        "gossip": ["Did you see who was at the tavern last night?", "I don't gossip.", "You do."],
        "the_well": ["The well's running low again.", "It always does this time of year.", "Someone should look at it."],
        "trade": ["Prices are up at the market.", "Everything's up.", "Not wages."],
        "old_times": ["When I was young the river ran twice as high.", "You say that every year.", "It was true every year."],
        "the_kid": ["Pip was chasing the chickens again.", "Kids.", "Someone should give that one a job."],
        "food": ["That bread this morning was good.", "Fresh at six, gone by nine.", "Save me a loaf tomorrow."],
    }

    def __init__(self, seed: int = 3) -> None:
        self.rng = random.Random(seed)
        self.calls = 0

    async def converse(self, a, b, topic, world):
        self.calls += 1
        if topic.startswith("news:"):
            news = topic[5:]
            lines = [f"Did you hear? {news}", "No! When?", "This morning, they say."]
        elif b.controlled or a.controlled:
            lines = ["You're new here, aren't you?", "Just passing through.", "Well. The tavern's that way."]
        else:
            lines = self.LINES.get(topic, ["Hello.", "Hello yourself."])
        return [(a.name if i % 2 == 0 else b.name, line) for i, line in enumerate(lines)]


class ClaudeNarrator:
    name = "claude"

    # Short in-character lines are a small job: Haiku answers in ~1 s at a fraction of the
    # cost. Pass a bigger model when the writing matters more than the latency.
    def __init__(self, model: str = "claude-haiku-4-5") -> None:
        import anthropic  # optional dependency path; imported here so the sim runs without a key

        self.client = anthropic.AsyncAnthropic()
        self.model = model
        self.calls = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.fallback = TemplateNarrator()

    SYSTEM = (
        "You write dialogue for a tiny village simulation. Two villagers meet and exchange 2 to 4 short lines "
        "(under 15 words each) about a given topic, in character, plain speech, no narration, no quotes. "
        "Format: one line per turn as `Name: words`. Nothing else."
    )

    async def converse(self, a, b, topic, world):
        import anthropic

        topic_text = TOPICS.get(topic, topic[5:] + " (news one of them just heard; the other hasn't)" if topic.startswith("news:") else topic)
        prompt = (
            f"Time: {world.clock.label()}. Weather: {world.weather}.\n"
            f"{a.name}, {a.role}, traits {', '.join(a.traits)}, feels {a.describe(world).get('feels', 'fine')}, remembers {list(a.memory)}.\n"
            f"{b.name}, {b.role}, traits {', '.join(b.traits)}, feels {b.describe(world).get('feels', 'fine')}, remembers {list(b.memory)}.\n"
            f"Topic: {topic_text}. {a.name} speaks first."
        )
        try:
            resp = await self.client.messages.create(
                model=self.model,
                max_tokens=300,
                system=self.SYSTEM,
                messages=[{"role": "user", "content": prompt}],
            )
        except (anthropic.APIStatusError, anthropic.APIConnectionError) as e:
            world.log(f"narrator fell back to templates ({type(e).__name__})")
            return await self.fallback.converse(a, b, topic, world)
        self.calls += 1
        self.input_tokens += resp.usage.input_tokens
        self.output_tokens += resp.usage.output_tokens
        text = next((blk.text for blk in resp.content if blk.type == "text"), "")
        out: list[tuple[str, str]] = []
        for line in text.splitlines():
            if ":" in line:
                who, _, said = line.partition(":")
                who = who.strip().strip("*").strip()
                if who in (a.name, b.name) and said.strip():
                    out.append((who, said.strip()))
        return out or await self.fallback.converse(a, b, topic, world)
