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

import json
import random
import re
from typing import Protocol

from .brain import TOPICS
from .npc import NPC
from .outcomes import Proposed
from .world import World


class Narrator(Protocol):
    name: str
    calls: int

    async def converse(self, a: NPC, b: NPC, topic: str, world: World) -> tuple[list[tuple[str, str]], Proposed]: ...


class TemplateNarrator:
    name = "template"

    async def reply(self, n: NPC, message: str, world: World) -> tuple[str, dict]:
        self.calls += 1
        canned = {
            "baker": "Bread's fresh if you want some. Otherwise I'm busy.",
            "farmer": "Hm. Can't stand about talking, the field won't wait.",
            "innkeeper": "Come in out of the weather and tell me everything.",
            "kid": "Are you from far away? Have you seen a dragon?",
            "merchant": "Interesting. And what's that worth to you?",
        }
        return canned.get(n.role.split(",")[0].split(" ")[0], "Aye. Well."), {}

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
        proposed = Proposed()
        if topic.startswith("news:"):
            news = topic[5:]
            lines = [f"Did you hear? {news}", "No! When?", "This morning, they say."]
        elif b.controlled or a.controlled:
            lines = ["You're new here, aren't you?", "Just passing through.", "Well. The tavern's that way."]
        else:
            lines = list(self.LINES.get(topic, ["Hello.", "Hello yourself."]))
            r = self.rng.random()
            if r < 0.25:
                what = self.rng.choice(["bring you a loaf tomorrow", "fix that fence for you", "save you a seat at the tavern tonight"])
                lines.append(f"I'll {what}.")
                proposed = Proposed(promise_by=b.name, promise_what=what)
            elif r < 0.45:
                others = [n for n in world.npcs if n not in (a, b) and not n.controlled]
                if others:
                    o = self.rng.choice(others)
                    what = self.rng.choice(["has been sneaking off to the mill", "owes half the village money", "hasn't paid for ale in a month"])
                    lines.append(f"Between us: {o.name} {what}.")
                    proposed = Proposed(rumor_about=o.name, rumor_what=f"{o.name} {what}")
        return [(a.name if i % 2 == 0 else b.name, line) for i, line in enumerate(lines)], proposed


class ClaudeNarrator:
    name = "claude"

    REPLY_SYSTEM = (
        "You voice one villager in a tiny village simulation, replying to a traveler who just spoke to them. "
        "Reply in character in 1 to 3 short sentences (under 40 words total), plain speech only: no narration, no stage "
        "directions, no asterisks, no quotes. "
        "Stay true to their traits, mood, what they remember, what they've heard, and how they feel about the traveler. "
        "Then add one final line: OUTCOME: {\"news\": \"a factual claim the traveler asserted about the village or a named villager, "
        "in third person with names, or null\", \"request\": \"what the traveler asked this villager to do, as a short verb phrase, or null\", "
        "\"agreed\": true or false (did the villager agree to the request, judging by your reply)}. Only report what was actually said."
    )

    async def reply(self, n: NPC, message: str, world: World) -> tuple[str, dict]:
        import anthropic

        me = n.describe(world)
        history = "\n".join(f"{who}: {text}" for who, text in n.chat[-8:])
        aff = n.affinity.get("traveler", 0.0)
        feeling = "trusts" if aff >= 0.4 else "distrusts" if aff <= -0.3 else "doesn't know well"
        prompt = (
            f"Time: {world.clock.label()}, weather {world.weather}.\n"
            f"You are {n.name}, {n.role}. Traits: {', '.join(n.traits)}. You feel: {me.get('feels')}. Mood {n.mood:.1f}/2.\n"
            f"You remember recently: {list(n.memory)}. You remember well: {n.long_term[-5:]}.\n"
            f"You have heard: {n.knows[-4:]}. You {feeling} the traveler.\n"
            f"Villagers you know: {', '.join(o.name for o in world.npcs if o is not n and not o.controlled)}.\n"
            + (f"Earlier with the traveler:\n{history}\n" if history else "")
            + f"The traveler says to you: {message}"
        )
        try:
            resp = await self.client.messages.create(model=self.model, max_tokens=250, system=self.REPLY_SYSTEM, messages=[{"role": "user", "content": prompt}])
        except (anthropic.APIStatusError, anthropic.APIConnectionError) as e:
            world.log(f"narrator fell back to templates ({type(e).__name__})")
            return await TemplateNarrator().reply(n, message, world)
        self.calls += 1
        self.input_tokens += resp.usage.input_tokens
        self.output_tokens += resp.usage.output_tokens
        text = next((blk.text for blk in resp.content if blk.type == "text"), "").strip()
        outcome: dict = {}
        said = []
        for line in text.splitlines():
            if line.strip().upper().startswith("OUTCOME:"):
                m = re.search(r"\{.*\}", line)
                if m:
                    try:
                        outcome = json.loads(m.group(0))
                    except json.JSONDecodeError:
                        outcome = {}
            elif line.strip():
                said.append(line.strip().removeprefix(f"{n.name}:").strip())
        spoken = re.sub(r"\*[^*]*\*", "", " ".join(said)).strip() or "..."  # drop any *stage directions*
        return spoken, {k: v for k, v in outcome.items() if v not in (None, "", "null")}

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
        "Format: one line per turn as `Name: words`. "
        "Sometimes (not always) a conversation produces a concrete promise by one speaker to do something "
        "specific for the other, or gossip about a named villager who is not present. After the lines, add one "
        'final line: OUTCOME: {"promise": {"by": "Name", "what": "short verb phrase"} or null, '
        '"rumor": {"about": "Name", "what": "one short sentence"} or null}. Only report what the lines actually say. '
        "In OUTCOME, always use the villager's name, never a pronoun, and only names from the villagers listed." 
    )

    async def converse(self, a, b, topic, world):
        import anthropic

        topic_text = TOPICS.get(topic, topic[5:] + " (news one of them just heard; the other hasn't)" if topic.startswith("news:") else topic)
        prompt = (
            f"Time: {world.clock.label()}. Weather: {world.weather}.\n"
            f"{a.name}, {a.role}, traits {', '.join(a.traits)}, feels {a.describe(world).get('feels', 'fine')}, remembers {list(a.memory)}.\n"
            f"{b.name}, {b.role}, traits {', '.join(b.traits)}, feels {b.describe(world).get('feels', 'fine')}, remembers {list(b.memory)}.\n"
            f"Topic: {topic_text}. {a.name} speaks first. "
            f"Other villagers (absent): {', '.join(n.name for n in world.npcs if n not in (a, b) and not n.controlled)}."
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
        proposed = Proposed()
        for line in text.splitlines():
            if line.strip().upper().startswith("OUTCOME:"):
                m = re.search(r"\{.*\}", line)
                if m:
                    try:
                        o = json.loads(m.group(0))
                    except json.JSONDecodeError:
                        o = {}
                    pr, ru = o.get("promise") or {}, o.get("rumor") or {}
                    proposed = Proposed(pr.get("by"), pr.get("what"), ru.get("about"), ru.get("what"))
                continue
            if ":" in line:
                who, _, said = line.partition(":")
                who = who.strip().strip("*").strip()
                if who in (a.name, b.name) and said.strip():
                    out.append((who, said.strip()))
        if not out:
            return await self.fallback.converse(a, b, topic, world)
        return out, proposed
