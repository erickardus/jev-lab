"""The bounded set of things an NPC can do. Jev picks one; code executes it.

Each Action knows whether it is available to an NPC right now, how to describe itself
for Jev's Choice criteria (in words, with distance), how to start (set a target), and
what happens on completion (effects on needs, memory). Adding a behavior = adding an
Action here; it appears in Jev's options automatically.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .npc import NPC
from .world import World


@dataclass
class Action:
    id: str
    label: str  # shown in the UI while doing it
    describe: Callable[[NPC, World], str]  # Jev's criteria text
    available: Callable[[NPC, World], bool]
    start: Callable[[NPC, World], None]  # sets npc.target / npc.action_label; may set duration via npc.busy_until
    on_arrive: Callable[[NPC, World, "Sim"], None]  # what to do at the target; sets busy_until for the stay
    on_complete: Callable[[NPC, World, "Sim"], None]
    interruptible: bool = True


def _dist(npc: NPC, world: World, place: str) -> str:
    return world.distance_word(npc.tile, world.places[place].anchor)


# ----------------------------------------------------------------------------- helpers

def go_to(place: str):
    def start(npc: NPC, world: World) -> None:
        npc.target = world.places[place].anchor
    return start


def stay(seconds: float):
    def on_arrive(npc: NPC, world: World, sim) -> None:
        npc.busy_until = sim.t + seconds
    return on_arrive


def noop(*_):
    pass


# ----------------------------------------------------------------------------- actions

def build_actions() -> list[Action]:
    acts: list[Action] = []

    # --- rest at home -------------------------------------------------------------
    def rest_arrive(npc, world, sim):
        hours = 6 if world.clock.hour >= 20 or world.clock.hour < 5 else 1.5
        npc.busy_until = sim.t + hours * world.clock.seconds_per_game_hour
        npc.action_label = "sleeping" if hours > 2 else "resting at home"

    def rest_done(npc, world, sim):
        npc.needs["energy"] = 0.05
        npc.remember("slept" if world.clock.hour < 9 else "had a rest at home")

    acts.append(Action(
        "go_home_rest", "heading home to rest",
        lambda n, w: f"Walk home ({_dist(n, w, n.home)}) and sleep or rest",
        lambda n, w: True,
        lambda n, w: go_to(n.home)(n, w), rest_arrive, rest_done,
    ))

    # --- eat at the tavern ---------------------------------------------------------
    def eat_done(npc, world, sim):
        npc.needs["hunger"] = 0.05
        npc.needs["social"] = max(0.0, npc.needs["social"] - 0.2)
        npc.remember("ate at the tavern")

    acts.append(Action(
        "eat_at_tavern", "eating at the tavern",
        lambda n, w: f"Walk to {w.places['tavern'].name} ({_dist(n, w, 'tavern')}) for a meal; {w.places['tavern'].describe(w)}",
        lambda n, w: True,
        go_to("tavern"), stay(25.0), eat_done,
    ))

    # --- buy bread ----------------------------------------------------------------
    def bread_done(npc, world, sim):
        npc.needs["hunger"] = max(0.0, npc.needs["hunger"] - 0.5)
        npc.remember("bought bread at the bakery")

    acts.append(Action(
        "buy_bread", "buying bread",
        lambda n, w: f"Walk to the bakery ({_dist(n, w, 'bakery')}) for bread; {w.places['bakery'].describe(w)}",
        lambda n, w: n.workplace != "bakery" and 6 <= w.clock.hour < 14,
        go_to("bakery"), stay(8.0), bread_done,
    ))

    # --- work -----------------------------------------------------------------------
    WORK_LABEL = {"field": "working the field", "bakery": "baking", "tavern": "tending the tavern", "market": "trading at the market", "well": "sitting by the well"}

    def work_arrive(npc, world, sim):
        npc.busy_until = sim.t + 2.5 * world.clock.seconds_per_game_hour
        npc.action_label = WORK_LABEL.get(npc.workplace, "working")

    def work_done(npc, world, sim):
        npc.needs["energy"] = min(1.0, npc.needs["energy"] + 0.15)
        npc.remember(f"spent a while {WORK_LABEL.get(npc.workplace, 'working')}")

    acts.append(Action(
        "work", "going to work",
        lambda n, w: f"Go to {w.places[n.workplace].name} ({_dist(n, w, n.workplace)}) and spend a while {WORK_LABEL.get(n.workplace, 'working')}",
        lambda n, w: n.workplace is not None and 6 <= w.clock.hour < 19,
        lambda n, w: go_to(n.workplace)(n, w), work_arrive, work_done,
    ))

    # --- fetch water ----------------------------------------------------------------
    acts.append(Action(
        "fetch_water", "fetching water",
        lambda n, w: f"Walk to the well ({_dist(n, w, 'well')}) to fetch water; {w.places['well'].describe(w)}",
        lambda n, w: n.workplace != "well",
        go_to("well"), stay(6.0), lambda n, w, s: n.remember("fetched water at the well"),
    ))

    # --- wander / square ----------------------------------------------------------
    def wander_start(npc, world):
        npc.target = world.random_walkable(npc.tile, 7)

    acts.append(Action(
        "wander", "wandering",
        lambda n, w: "Stroll around nearby with no particular aim",
        lambda n, w: True,
        wander_start, stay(3.0), lambda n, w, s: None,
    ))

    acts.append(Action(
        "go_to_square", "heading to the square",
        lambda n, w: f"Go to the market square ({_dist(n, w, 'market')}) to see who's around; {w.places['market'].describe(w)}",
        lambda n, w: n.at_place != "market",
        go_to("market"), stay(10.0), lambda n, w, s: n.remember("hung around the square"),
    ))

    # --- stay -----------------------------------------------------------------------
    acts.append(Action(
        "stay", "staying put",
        lambda n, w: "Stay where they are a while longer",
        lambda n, w: True,
        lambda n, w: setattr(n, "target", n.tile), stay(8.0), lambda n, w, s: None,
    ))

    return acts


def talk_action(other: NPC) -> Action:
    """A dynamic action: walk over to `other` and talk. One per other NPC in the village."""

    def describe(n: NPC, w: World) -> str:
        where = w.places[other.at_place].name if other.at_place and w.places[other.at_place].kind != "home" else "on the road" if not other.at_place else "at home"
        return f"Walk over to {other.name} the {other.role} ({w.distance_word(n.tile, other.tile)}, {where}) and have a chat"

    def available(n: NPC, w: World) -> bool:
        # can't chat with someone asleep at home, or already mid-conversation
        return other.talking_to is None and not (other.at_place == other.home and other.action == "go_home_rest")

    def start(n: NPC, w: World) -> None:
        n.target = other.tile
        n.talking_to = other.id  # intent; the conversation starts on arrival

    return Action(f"talk_to_{other.id}", f"going to talk to {other.name}", describe, available, start, noop, noop)
