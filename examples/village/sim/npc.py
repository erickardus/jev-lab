"""An NPC: who they are (static), how they feel (needs), what they remember, what they're doing."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field


def need_word(kind: str, v: float) -> str:
    """Jev handles words better than numbers, so needs are always described, never quoted."""
    scale = {
        "hunger": ["full", "peckish", "hungry", "starving"],
        "energy": ["rested", "a bit tired", "tired", "exhausted"],
        "social": ["content", "could use some company", "lonely", "very lonely"],
    }[kind]
    return scale[0 if v < 0.3 else 1 if v < 0.6 else 2 if v < 0.85 else 3]


@dataclass
class NPC:
    id: str
    name: str
    role: str
    traits: list[str]
    home: str  # place id
    workplace: str | None  # place id
    color: str
    x: float
    y: float
    # needs rise toward 1.0 over time; actions bring them down
    needs: dict[str, float] = field(default_factory=lambda: {"hunger": 0.4, "energy": 0.2, "social": 0.5})
    need_rates: dict[str, float] = field(default_factory=lambda: {"hunger": 0.022, "energy": 0.009, "social": 0.016})
    memory: deque = field(default_factory=lambda: deque(maxlen=6))
    # current activity, written by actions
    action: str | None = None
    action_label: str = "standing around"
    target: tuple[int, int] | None = None
    path: list[tuple[int, int]] = field(default_factory=list)
    busy_until: float = 0.0  # sim seconds; the action completes at this time
    talking_to: str | None = None
    bubble: str | None = None
    bubble_until: float = 0.0
    # the last decision, for the UI
    probs: dict[str, float] = field(default_factory=dict)
    confidence: float = 0.0
    mood: float = 1.0  # Score 0..2: gloomy .. cheerful
    topic: str | None = None
    deciding: bool = False
    speed: float = 3.0  # tiles per second
    _chase: int = 0  # how many times we re-pathed toward a moving conversation partner
    controlled: bool = False  # the player: no brain, no needs; villagers still see them
    current: object = None  # the Action being executed (kept here, not in a shared registry)
    knows: list[str] = field(default_factory=list)  # news they have heard, oldest first
    affinity: dict[str, float] = field(default_factory=dict)  # other id -> how much they like them
    last_options: dict[str, str] = field(default_factory=dict)  # option id -> description Jev was given
    share_news: float = 0.0  # Noul: would pass on news when chatting
    help_p: float = 0.0  # Noul: would drop everything to help with a known incident
    # beliefs: what this villager has seen, not what is true
    last_seen: dict[str, tuple[str, str, float]] = field(default_factory=dict)  # other id -> (where, hhmm, t)
    sees: set = field(default_factory=set)  # ids visible right now (recomputed every tick)
    commitments: list[dict] = field(default_factory=list)  # {"id","to","what","deadline_t"}
    forced: str | None = None  # an incident forcing behavior (hiding, ill in bed)
    # history: everything, forever; and what stuck
    journal: list[dict] = field(default_factory=list)  # {"day","hhmm","text","significant"}
    long_term: list[str] = field(default_factory=list)  # significant memories, kept all run (cap 10)
    trait_log: list[dict] = field(default_factory=list)  # {"day","change","p"}
    original_traits: list[str] = field(default_factory=list)
    stamp: object = None  # set by the sim: callable -> (day, hhmm)
    chat: list[tuple[str, str]] = field(default_factory=list)  # exchanges with the traveler, (who, text), last 10

    @property
    def tile(self) -> tuple[int, int]:
        return (int(round(self.x)), int(round(self.y)))

    @property
    def at_place(self) -> str | None:
        return getattr(self, "_at_place", None)

    @at_place.setter
    def at_place(self, v: str | None) -> None:
        self._at_place = v

    @property
    def idle(self) -> bool:
        return self.action is None and not self.path

    def remember(self, text: str, significant: bool = False) -> None:
        """Short-term memory (what Jev sees as `recently`), the journal (everything), and, for
        significant events, long-term memory that stays with them for the whole run."""
        if not self.memory or self.memory[-1] != text:
            self.memory.append(text)
        day, hhmm = self.stamp() if self.stamp else (1, "")
        self.journal.append({"day": day, "hhmm": hhmm, "text": text, "significant": significant})
        del self.journal[:-400]
        if significant and text not in self.long_term:
            self.long_term.append(text)
            del self.long_term[:-10]

    def today(self, day: int, limit: int = 25) -> list[str]:
        return [f"{j['hhmm']} {j['text']}" for j in self.journal if j["day"] == day][-limit:]

    def tick_needs(self, dt: float, asleep: bool = False) -> None:
        for k, r in self.need_rates.items():
            if asleep and k == "energy":
                continue
            self.needs[k] = min(1.0, self.needs[k] + r * dt)

    def where_word(self, world) -> str:
        here = world.places.get(self.at_place) if self.at_place else None
        if here is None:
            return "on the road"
        return "home" if here.kind == "home" and here.owner == self.id else ("someone's house" if here.kind == "home" else here.name)

    def describe(self, world) -> dict:
        """What Jev sees about this villager, in words. Observed facts and their own state."""
        if self.controlled:
            return {"role": self.role, "traits": self.traits, "at": self.where_word(world), "doing": self.action_label, "recently": list(self.memory)}
        d = {
            "role": self.role,
            "traits": self.traits,
            "feels": {k: need_word(k, v) for k, v in self.needs.items()},
            "at": self.where_word(world),
            "doing": self.action_label,
            "recently": list(self.memory),
        }
        friends = [world.by_id[o].name for o, a in self.affinity.items() if a >= 0.4 and o in world.by_id]
        dislikes = [world.by_id[o].name for o, a in self.affinity.items() if a <= -0.3 and o in world.by_id]
        if friends:
            d["gets_along_with"] = friends
        if dislikes:
            d["on_bad_terms_with"] = dislikes
        if self.knows:
            d["has_heard"] = self.knows[-4:]
        if self.commitments:
            d["promised"] = [f"{c['what']} (to {world.by_id[c['to']].name})" for c in self.commitments if c["to"] in world.by_id]
        if self.long_term:
            d["remembers_well"] = self.long_term[-6:]
        if self.forced:
            d["condition"] = self.forced
        return d

    def describe_other(self, other: "NPC", world) -> dict:
        """Another villager as *this one* knows them: seen now, remembered, or unknown."""
        base = {"role": other.role}
        if other.id in self.sees:
            base["at"] = other.where_word(world)
            base["doing"] = other.action_label
            if other.talking_to and other.talking_to in world.by_id:
                base["doing"] = f"talking with {world.by_id[other.talking_to].name}"
        elif other.id in self.last_seen:
            where, hhmm, _ = self.last_seen[other.id]
            base["last_seen"] = f"{where} around {hhmm}"
        else:
            base["last_seen"] = "not seen today"
        a = self.affinity.get(other.id, 0.0)
        if a >= 0.4:
            base["relationship"] = "friend"
        elif a <= -0.3:
            base["relationship"] = "on bad terms"
        return base

    def snapshot(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "role": self.role,
            "traits": self.traits,
            "color": self.color,
            "x": self.x,
            "y": self.y,
            "action": self.action,
            "action_label": self.action_label,
            "needs": {k: round(v, 2) for k, v in self.needs.items()},
            "needs_words": {k: need_word(k, v) for k, v in self.needs.items()},
            "probs": {k: round(v, 2) for k, v in sorted(self.probs.items(), key=lambda kv: -kv[1])[:4]},
            "confidence": round(self.confidence, 2),
            "mood": round(self.mood, 2),
            "bubble": self.bubble,
            "talking_to": self.talking_to,
            "memory": list(self.memory),
            "deciding": self.deciding,
            "controlled": self.controlled,
            "knows": list(self.knows),
            "affinity": {k: round(v, 2) for k, v in self.affinity.items()},
            "options": self.last_options,
            "all_probs": {k: round(v, 3) for k, v in self.probs.items()},
            "asleep": self.action == "go_home_rest" and self.at_place == self.home and self.busy_until > 0,
            "moving": bool(self.path),
            "help_p": round(self.help_p, 2),
            "commitments": [{"to": c["to"], "what": c["what"]} for c in self.commitments],
            "last_seen": {k: f"{v[0]} · {v[1]}" for k, v in self.last_seen.items()},
            "sees": sorted(self.sees),
            "forced": self.forced,
            "long_term": list(self.long_term),
            "journal": self.journal[-30:],
            "trait_log": self.trait_log,
            "original_traits": self.original_traits,
        }


def traveler(x: float, y: float) -> NPC:
    """The player. Villagers see 'the traveler'; nobody decides for them."""
    return NPC("traveler", "Traveler", "traveler passing through, new to the village", ["quiet", "curious"],
               "market", None, "#3fbf7f", x, y, controlled=True)


def default_cast() -> list[NPC]:
    """Six villagers with enough contrast that their choices should differ."""
    return [
        NPC("mira", "Mira", "baker", ["cheerful", "gossipy", "early riser"], "home_a", "bakery", "#e4b04a", 4, 4),
        NPC("bram", "Bram", "farmer", ["quiet", "hardworking", "hungry by noon"], "home_b", "field", "#6f9a4a", 11, 4),
        NPC("sela", "Sela", "innkeeper", ["warm", "nosy", "night owl"], "home_c", "tavern", "#c25a7a", 24, 4),
        NPC("tom", "Old Tom", "retired fisherman", ["grumpy", "storyteller", "sits by the well"], "home_d", "well", "#8a8fa8", 4, 9),
        NPC("nell", "Nell", "merchant", ["ambitious", "curious", "always counting"], "home_e", "market", "#4a86c8", 29, 9),
        NPC("pip", "Pip", "kid", ["restless", "playful", "follows adults around"], "home_f", None, "#e07a3a", 4, 14),
    ]
