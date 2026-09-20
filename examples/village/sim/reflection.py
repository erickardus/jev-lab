"""Nightly reflection: the day's history changes who a villager is.

When a villager goes to sleep, Jev reads what happened to them today and their long-term
memories and judges how it changed them: more wary or more trusting, more sociable or
more withdrawn, more bitter or more generous, or not at all. Code applies the drift to
their traits, which every future decision sees. Change is slow (one step per night, only
when Jev is fairly sure), cumulative, and recorded in the chronicle, so a villager at day
five is a different person from day one for reasons you can read.
"""

from __future__ import annotations

from typesafe_sdk import AsyncTypeSafeClient, Choice

from .npc import NPC

# direction -> (trait to add, trait it displaces)
DRIFT = {
    "more_wary": ("wary of others", "trusting"),
    "more_trusting": ("trusting", "wary of others"),
    "more_sociable": ("seeks company", "withdrawn"),
    "more_withdrawn": ("withdrawn", "seeks company"),
    "more_bitter": ("bitter", "generous"),
    "more_generous": ("generous", "bitter"),
    "more_anxious": ("anxious", "easygoing"),
    "more_easygoing": ("easygoing", "anxious"),
}
CONFIDENCE_TO_CHANGE = 0.45
MAX_TRAITS = 6


async def reflect(client: AsyncTypeSafeClient, n: NPC, day: int) -> tuple[str, float, float] | None:
    today = n.today(day)
    if len(today) < 3:
        return None
    state = {
        "me": {"name": n.name, "role": n.role, "traits": n.traits, "remembers_well": n.long_term[-8:]},
        "today": today,
    }
    q = Choice(
        instructions=(
            f"Looking back on `today`, and on what `me` ({n.name}) remembers well, how has this day changed them as a person? "
            "Pick the single strongest change, or unchanged if it was an ordinary day."
        ),
        criteria={
            "more_wary": "Someone let them down, lied about them, or ignored them when they needed help; they trust people less",
            "more_trusting": "People helped them, kept their word, or stood by them; they trust people more",
            "more_sociable": "Good company and conversation made the day; they want more of it",
            "more_withdrawn": "Conflict, gossip about them, or being alone too long; they want to keep to themselves",
            "more_bitter": "Bad luck, loss, or unfairness that they resent",
            "more_generous": "They helped or were helped, and it felt right",
            "more_anxious": "Fire, illness, a missing child, danger; they are on edge",
            "more_easygoing": "Things worked out; they feel settled",
            "unchanged": "An ordinary day: work, food, a chat, sleep",
        },
    )
    resp = await client.system_one(state=state, questions={"change": q}, timeout=10.0)
    c = resp.choices["change"]
    return c.choice, c.probabilities.get(c.choice, 0.0), c.confidence


def apply(n: NPC, change: str, p: float, day: int) -> str | None:
    if change == "unchanged" or change not in DRIFT:
        return None
    add, drop = DRIFT[change]
    if drop in n.traits:
        n.traits.remove(drop)
    if add not in n.traits:
        n.traits.append(add)
    # keep the list short: evolved traits displace the oldest evolved one first
    while len(n.traits) > MAX_TRAITS:
        evolved = [t for t in n.traits if t not in n.original_traits and t != add]
        n.traits.remove(evolved[0] if evolved else n.traits[0])
    n.trait_log.append({"day": day, "change": change, "p": round(p, 2)})
    n.remember(f"I've grown {change.replace('more_', 'more ')}", significant=True)
    return add
