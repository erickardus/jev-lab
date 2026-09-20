"""The map, the places on it, pathfinding, and the clock. All deterministic."""

from __future__ import annotations

import heapq
import random
from dataclasses import dataclass, field

# Legend: '#' tree, '~' water, 'H' house wall, '.' grass, ':' path,
# letters = anchor tiles for places (walkable). a-f are home doors of NPCs 0-5.
MAP = """\
#################################
#..............~~~..............#
#..HHH....HHH...~~~....HHH......#
#..HaH....HbH...~~~....HcH......#
#...:......:.....~......:.......#
#...::::::::::::::::::::::......#
#.......:......:.........:......#
#..HHH..:...HHHHHHH......:..HHH.#
#..HdH..:...HHHTHHH......:..HeH.#
#...:...:......:.........:...:..#
#...::::::::::M::::::W:::::::::.#
#.......:......:................#
#..HHH..:..HHHHH................#
#..HfH..:..HHBHH......FFFFFFFF..#
#...:...:....:........FFFFFFFF..#
#...::::::::::........FFFFFFFF..#
#.............:.......FFFFFFFF..#
#.............:::::::::.........#
#...............................#
#################################"""

WALKABLE = set(".:abcdefTBWMF")


@dataclass
class Place:
    id: str
    name: str
    anchor: tuple[int, int]
    kind: str  # home | tavern | bakery | well | field | market
    owner: str | None = None  # npc id for homes

    def describe(self, world: "World") -> str:
        here = [n.name for n in world.npcs if n.at_place == self.id]
        base = {
            "tavern": "warm, serves food and drink",
            "bakery": "sells bread in the morning",
            "well": "the village well",
            "field": "the wheat field",
            "market": "the market square",
            "home": "a small house",
        }[self.kind]
        if self.kind == "tavern":
            base += ", open" if 10 <= world.clock.hour < 24 or world.clock.hour < 1 else ", closed"
        if self.kind == "bakery":
            base += ", open" if 6 <= world.clock.hour < 14 else ", closed"
        if here:
            base += "; " + ", ".join(here) + (" is" if len(here) == 1 else " are") + " here"
        if self.kind == "field" and world.weather == "raining":
            base += "; muddy in the rain"
        return base


@dataclass
class Clock:
    minutes: float = 7 * 60.0  # start 07:00
    seconds_per_game_hour: float = 15.0  # a full day in 6 real seconds * 60 = 6 min

    def tick(self, dt: float) -> None:
        self.minutes = (self.minutes + dt / self.seconds_per_game_hour * 60.0) % (24 * 60)

    @property
    def hour(self) -> float:
        return self.minutes / 60.0

    def hhmm(self) -> str:
        return f"{int(self.minutes // 60):02d}:{int(self.minutes % 60):02d}"

    def label(self) -> str:
        h = self.hour
        if h < 5:
            return "the middle of the night"
        if h < 7:
            return "dawn"
        if h < 11:
            return "morning"
        if h < 14:
            return "midday"
        if h < 17:
            return "afternoon"
        if h < 20:
            return "evening, getting dark"
        return "night"

    @property
    def daylight(self) -> float:
        """0 (dark) .. 1 (full day), for the renderer."""
        h = self.hour
        if 7 <= h <= 18:
            return 1.0
        if 5 <= h < 7:
            return (h - 5) / 2
        if 18 < h <= 21:
            return 1 - (h - 18) / 3
        return 0.0


class World:
    def __init__(self, seed: int = 7) -> None:
        self.rng = random.Random(seed)
        self.rows = MAP.splitlines()
        self.h = len(self.rows)
        self.w = len(self.rows[0])
        self.clock = Clock()
        self.places: dict[str, Place] = {}
        self.npcs: list = []  # filled by the simulation
        self.by_id: dict = {}
        self.events: list[str] = []
        self.weather: str = "clear"
        self.news: list[dict] = []  # {"text", "at", "since"}
        self._index_places()

    # ------------------------------------------------------------------ places
    def _index_places(self) -> None:
        named = {"T": ("tavern", "The Wet Boot", "tavern"), "B": ("bakery", "Mira's bakery", "bakery"),
                 "W": ("well", "the well", "well"), "M": ("market", "the market square", "market")}
        field_tiles = []
        for y, row in enumerate(self.rows):
            for x, ch in enumerate(row):
                if ch in named:
                    pid, name, kind = named[ch]
                    self.places[pid] = Place(pid, name, (x, y), kind)
                elif ch in "abcdef":
                    self.places[f"home_{ch}"] = Place(f"home_{ch}", "home", (x, y), "home")
                elif ch == "F":
                    field_tiles.append((x, y))
        if field_tiles:
            cx = sum(x for x, _ in field_tiles) // len(field_tiles)
            cy = sum(y for _, y in field_tiles) // len(field_tiles)
            self.places["field"] = Place("field", "the field", (cx, cy), "field")

    def tile(self, x: int, y: int) -> str:
        return self.rows[y][x] if 0 <= y < self.h and 0 <= x < self.w else "#"

    def walkable(self, x: int, y: int) -> bool:
        return self.tile(x, y) in WALKABLE

    def random_walkable(self, near: tuple[int, int] | None = None, radius: int = 6) -> tuple[int, int]:
        for _ in range(200):
            if near:
                x = near[0] + self.rng.randint(-radius, radius)
                y = near[1] + self.rng.randint(-radius, radius)
            else:
                x, y = self.rng.randrange(self.w), self.rng.randrange(self.h)
            if self.walkable(x, y) and self.tile(x, y) in ".:":
                return (x, y)
        return near or (15, 10)

    def place_at(self, x: int, y: int) -> str | None:
        for p in self.places.values():
            if abs(p.anchor[0] - x) + abs(p.anchor[1] - y) <= 1:
                return p.id
        if self.tile(x, y) == "F":
            return "field"
        return None

    def distance_word(self, a: tuple[int, int], b: tuple[int, int]) -> str:
        d = abs(a[0] - b[0]) + abs(a[1] - b[1])
        if d <= 2:
            return "right here"
        if d <= 8:
            return "nearby"
        if d <= 16:
            return "a short walk away"
        return "across the village"

    # ------------------------------------------------------------- pathfinding
    def path(self, start: tuple[int, int], goal: tuple[int, int]) -> list[tuple[int, int]]:
        """A* on the grid; paths prefer roads (':') slightly. Returns tiles after `start`."""
        if start == goal:
            return []
        if not self.walkable(*goal):
            goal = self._nearest_walkable(goal)

        def h(p):
            return abs(p[0] - goal[0]) + abs(p[1] - goal[1])

        open_: list[tuple[float, tuple[int, int]]] = [(h(start), start)]
        came: dict[tuple[int, int], tuple[int, int]] = {}
        g = {start: 0.0}
        while open_:
            _, cur = heapq.heappop(open_)
            if cur == goal:
                out = []
                while cur != start:
                    out.append(cur)
                    cur = came[cur]
                return out[::-1]
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                nxt = (cur[0] + dx, cur[1] + dy)
                if not self.walkable(*nxt):
                    continue
                step = 0.8 if self.tile(*nxt) == ":" else 1.0
                ng = g[cur] + step
                if ng < g.get(nxt, 1e9):
                    g[nxt] = ng
                    came[nxt] = cur
                    heapq.heappush(open_, (ng + h(nxt), nxt))
        return []

    def _nearest_walkable(self, p: tuple[int, int]) -> tuple[int, int]:
        for r in range(1, 6):
            for dx in range(-r, r + 1):
                for dy in range(-r, r + 1):
                    q = (p[0] + dx, p[1] + dy)
                    if self.walkable(*q):
                        return q
        return p

    # ------------------------------------------------------------------ events
    def log(self, text: str) -> None:
        self.events.append(f"[{self.clock.hhmm()}] {text}")
        del self.events[:-40]

    def snapshot_map(self) -> dict:
        return {
            "w": self.w,
            "h": self.h,
            "rows": self.rows,
            "places": [{"id": p.id, "name": p.name, "x": p.anchor[0], "y": p.anchor[1], "kind": p.kind} for p in self.places.values()],
        }
