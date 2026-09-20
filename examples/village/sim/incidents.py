"""Incidents: things that go wrong and need someone to step up.

Plain news ("a boat was seen") is passive. An incident creates a *job* — carry water to
the fire, find Pip, bring Old Tom food — that becomes an available action for every
villager who knows about it. Who steps up is Jev's call, through the normal action
Choice plus a speculative Noul ("would they drop what they're doing to help?") that the
panel shows for everyone at once. Deadlines, progress counts, and consequences are code.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

from .actions import Action, go_to
from .npc import NPC
from .world import World


@dataclass
class Incident:
    id: str
    kind: str
    text: str  # what witnesses see / what spreads as news
    place: str  # place id the job happens at (or "search")
    job: str  # verb phrase for the action description
    needed: int  # helper completions to resolve
    deadline_t: float
    victim: str | None = None
    created_t: float = 0.0
    progress: int = 0
    helpers: list[str] = field(default_factory=list)
    resolved: bool = False
    failed: bool = False

    @property
    def open(self) -> bool:
        return not (self.resolved or self.failed)


KINDS = {
    "fire": dict(text="the bakery is on fire!", place="bakery", job="carry water from the well to the bakery fire", needed=3, seconds=75.0, victim="mira"),
    "pip_missing": dict(text="nobody can find Pip", place="search", job="search the village for Pip", needed=1, seconds=110.0, victim="pip"),
    "tom_ill": dict(text="Old Tom is ill in bed", place="home_d", job="bring Old Tom some food", needed=1, seconds=120.0, victim="tom"),
    "thief": dict(text="a thief snatched coins from Nell's stall", place="field", job="chase the thief toward the field", needed=1, seconds=45.0, victim="nell"),
}


def spawn(kind: str, world: World, t: float, n: int) -> Incident:
    k = KINDS[kind]
    return Incident(f"inc{n}", kind, k["text"], k["place"], k["job"], k["needed"], t + k["seconds"], k["victim"], created_t=t)


def on_spawn(inc: Incident, sim) -> list[NPC]:
    """World effects when it starts; returns the witnesses."""
    w = sim.world
    if inc.kind == "fire":
        w.place_state["bakery"] = "on fire"
        witnesses = [n for n in sim.villagers if n.at_place == "bakery" or abs(n.x - w.places["bakery"].anchor[0]) + abs(n.y - w.places["bakery"].anchor[1]) <= 8]
        mira = sim.by_id["mira"]
        if mira not in witnesses:
            witnesses.append(mira)
    elif inc.kind == "pip_missing":
        pip = sim.by_id["pip"]
        sim.force(pip, "hiding somewhere", w.random_walkable(None), 100.0)
        witnesses = [n for n in sim.villagers if n.id != "pip" and n.affinity.get("pip", 0) > 0] or [sim.by_id["mira"]]
    elif inc.kind == "tom_ill":
        tom = sim.by_id["tom"]
        sim.force(tom, "ill in bed", w.places[tom.home].anchor, 130.0)
        witnesses = [n for n in sim.villagers if "tom" in n.sees and n.id != "tom"] or [sim.by_id["sela"]]
    else:  # thief
        nell = sim.by_id["nell"]
        witnesses = [n for n in sim.villagers if n.at_place == "market" or "nell" in n.sees] + [nell]
        witnesses = list({n.id: n for n in witnesses}.values())
    return witnesses


def job_action(inc: Incident, sim) -> Action:
    w = sim.world

    def describe(n: NPC, world: World) -> str:
        if inc.place == "search":
            return f"Drop what they're doing and {inc.job} (urgent; {inc.needed - inc.progress} still needed)"
        dist = world.distance_word(n.tile, world.places[inc.place].anchor)
        return f"Drop what they're doing and {inc.job} ({dist}; urgent; {inc.needed - inc.progress} more needed)"

    def available(n: NPC, world: World) -> bool:
        return inc.open and inc.text in n.knows and n.id != inc.victim and not (inc.kind == "pip_missing" and n.role == "kid")

    def start(n: NPC, world: World) -> None:
        if inc.place == "search":
            n.target = world.random_walkable(None)
        else:
            n.target = world.places[inc.place].anchor
        n.action_label = inc.job.split(" toward")[0]

    def on_arrive(n: NPC, world: World, s) -> None:
        if inc.kind == "pip_missing":
            pip = s.by_id["pip"]
            if abs(n.x - pip.x) + abs(n.y - pip.y) <= 4:
                n.busy_until = s.t + 2.0
            else:  # keep looking: pick another spot
                n.target = world.random_walkable(None)
                n.path = world.path(n.tile, n.target)
                n._chase += 1
                if n._chase > 5 or not n.path:
                    n.busy_until = s.t + 0.1
                    n._gave_up = True
            return
        n.busy_until = s.t + (8.0 if inc.kind == "fire" else 5.0)

    def on_complete(n: NPC, world: World, s) -> None:
        if getattr(n, "_gave_up", False):
            n._gave_up = False
            n.remember(f"looked for Pip and couldn't find him")
            return
        if inc.kind == "pip_missing":
            pip = s.by_id["pip"]
            if abs(n.x - pip.x) + abs(n.y - pip.y) > 4:
                return
        inc.progress += 1
        if n.id not in inc.helpers:
            inc.helpers.append(n.id)
        n.remember(f"helped: {inc.job}", significant=True)
        s.world.log(f"{n.name} helps ({inc.kind}): {inc.progress}/{inc.needed}")
        if inc.progress >= inc.needed:
            resolve(inc, s)

    return Action(f"help_{inc.id}", inc.job, describe, available, start, on_arrive, on_complete, interruptible=False)


def resolve(inc: Incident, sim) -> None:
    inc.resolved = True
    w = sim.world
    helpers = [sim.by_id[h] for h in inc.helpers]
    names = ", ".join(h.name for h in helpers) or "nobody"
    if inc.kind == "fire":
        w.place_state["bakery"] = "smoke-blackened but standing"
        text = f"the bakery fire was put out by {names}"
    elif inc.kind == "pip_missing":
        sim.unforce(sim.by_id["pip"])
        text = f"{names} found Pip"
    elif inc.kind == "tom_ill":
        text = f"{names} brought food to Old Tom while he was ill"
    else:
        text = f"{names} chased off the thief"
    w.log(f"resolved: {text}")
    victim = sim.by_id.get(inc.victim or "")
    for h in helpers:
        h.mood = min(2.0, h.mood + 0.5)
        if victim and victim is not h:
            victim.affinity[h.id] = min(1.0, victim.affinity.get(h.id, 0.0) + 0.4)
            victim.remember(f"{h.name} helped me when {inc.text.rstrip('!')}", significant=True)
    sim.publish_news(text, witnesses=helpers + ([victim] if victim else []))


def fail(inc: Incident, sim) -> None:
    inc.failed = True
    w = sim.world
    victim = sim.by_id.get(inc.victim or "")
    if inc.kind == "fire":
        w.place_state["bakery"] = "burnt out; closed"
        text = "the bakery burned; nobody put the fire out in time"
    elif inc.kind == "pip_missing":
        sim.unforce(sim.by_id["pip"])
        text = "Pip wandered back on his own after nobody came looking"
    elif inc.kind == "tom_ill":
        sim.unforce(sim.by_id["tom"])
        text = "Old Tom was ill for a day and nobody brought him anything"
    else:
        text = "the thief got away with Nell's coins"
    w.log(f"failed: {text}")
    if victim:
        victim.mood = max(0.0, victim.mood - 0.8)
        victim.remember(text, significant=True)
        for n in sim.villagers:
            if n is not victim and inc.text in n.knows and n.id not in inc.helpers:
                victim.affinity[n.id] = max(-1.0, victim.affinity.get(n.id, 0.0) - 0.25)  # knew and didn't come
    sim.publish_news(text, witnesses=[victim] if victim else [sim.world.rng.choice(sim.villagers)])


COOLDOWN = 400.0  # sim seconds before the same kind of incident can recur (~a game day)


def pick_kind(rng: random.Random, sim) -> str | None:
    if not (7 <= sim.world.clock.hour < 20):  # incidents happen while the village is awake
        return None
    recent = {i.kind for i in sim.incidents if i.open or sim.t - i.created_t < COOLDOWN}
    choices = [k for k in KINDS if k not in recent]
    if "fire" in choices and sim.world.place_state.get("bakery", "").startswith("burnt"):
        choices.remove("fire")
    if "tom_ill" in choices and sim.by_id["tom"].forced:
        choices.remove("tom_ill")
    if "pip_missing" in choices and sim.by_id["pip"].forced:
        choices.remove("pip_missing")
    return rng.choice(choices) if choices else None
