"""Conversation outcomes: the LLM proposes, Jev verifies, code applies.

The narrator writes the lines and may propose a structured outcome (a promise, a rumor
about a third villager). That is generation, and generation confabulates, so nothing is
applied until Jev has read the lines and answered typed questions: did the promise
actually get made, who is the rumor about, how did the conversation go. Then code turns
the verified outcome into facts the world runs on: a commitment with a deadline, a rumor
in the news system with a subject, an affinity change.
"""

from __future__ import annotations

from dataclasses import dataclass

from typesafe_sdk import AsyncTypeSafeClient, Choice, Noul, Score

from .npc import NPC


@dataclass
class Proposed:
    promise_by: str | None = None  # npc name
    promise_what: str | None = None
    rumor_about: str | None = None  # npc name
    rumor_what: str | None = None


def by_name_id(by_name: dict[str, NPC], npc_id: str) -> str:
    return next((n.name for n in by_name.values() if n.id == npc_id), npc_id)


@dataclass
class Outcome:
    tone: float  # Score 0 tense .. 2 warm
    tone_conf: float
    promise: tuple[str, str] | None = None  # (by id, what)
    rumor: tuple[str, str] | None = None  # (about id, what)
    p_promise: float = 0.0
    p_rumor: float = 0.0
    fulfilled: list[str] = None  # commitment ids carried out during this exchange


async def judge(client: AsyncTypeSafeClient, a: NPC, b: NPC, lines: list[tuple[str, str]], proposed: Proposed, by_name: dict[str, NPC],
                commitments: list[dict] | None = None) -> Outcome:
    state = {
        "speakers": {a.name: a.role, b.name: b.role},
        "dialogue": [f"{who}: {text}" for who, text in lines],
    }
    commitments = commitments or []
    if commitments:
        state["standing_promises"] = {c["id"]: f"{by_name_id(by_name, c['who'])} promised {by_name_id(by_name, c['to'])} to {c['what']}" for c in commitments}
    qs: dict = {
        "tone": Score(
            instructions="How did this conversation go between the two speakers, judging only by the `dialogue`?",
            criteria=[
                "Tense or unfriendly: an accusation, a refusal, sarcasm, or someone leaves annoyed",
                "Neutral: ordinary talk, nothing much changes between them",
                "Warm: they help, agree, joke, or clearly enjoy each other's company",
            ],
        ),
    }
    if proposed.promise_by and proposed.promise_what:
        state["claimed_promise"] = {"by": proposed.promise_by, "what": proposed.promise_what}
        qs["promise_real"] = Noul(
            instructions="In the `dialogue`, the speaker named in `claimed_promise.by` actually commits to do what `claimed_promise.what` says",
            criteria={
                "true": "They say they will do it, agree to a request for it, or offer it unprompted",
                "false": "It is only mentioned, joked about, refused, or said by the other speaker",
            },
        )
    if proposed.rumor_about and proposed.rumor_what:
        state["claimed_rumor"] = {"about": proposed.rumor_about, "what": proposed.rumor_what}
        others = {n.name: n.role for n in by_name.values() if n.name not in (a.name, b.name)}
        qs["rumor_real"] = Noul(
            instructions="In the `dialogue`, the speakers say or imply what `claimed_rumor.what` says about the conduct or character of the villager named in `claimed_rumor.about`, who is not present",
            criteria={
                "true": "The claim is about what that villager did, does, or is like (lazy, dishonest, sneaking off, owing money, unreliable), stated, hinted, or passed on as something heard",
                "false": "Not said, said about someone else or a speaker, or merely an event that happened to them (their bakery caught fire, they fell ill) rather than something about them",
            },
        )
        qs["rumor_subject"] = Choice(
            instructions="Who is the `dialogue` gossiping about, other than the two speakers?",
            criteria={**{name: role for name, role in others.items()}, "nobody": "They are not talking about an absent villager"},
        )
    for c in commitments:
        qs[f"done|{c['id']}"] = Noul(
            instructions=f"During this `dialogue`, the promise `standing_promises.{c['id']}` is actually carried out: the thing is handed over, done, or delivered right now",
            criteria={
                "true": "One speaker gives, shows, hands over, or completes the promised thing in the exchange, or both treat it as done",
                "false": "It is only mentioned, asked about, promised again, postponed, or excused",
            },
        )
    resp = await client.system_one(state=state, questions=qs, timeout=10.0)
    tone = resp.scores["tone"]
    out = Outcome(tone.score, tone.confidence)
    out.fulfilled = [c["id"] for c in commitments if resp.nouls[f"done|{c['id']}"].noul >= 0.6]
    if "promise_real" in resp.nouls:
        out.p_promise = resp.nouls["promise_real"].noul
        who = by_name.get(proposed.promise_by or "")
        if out.p_promise >= 0.6 and who is not None and who.id in (a.id, b.id):
            out.promise = (who.id, proposed.promise_what)
    if "rumor_real" in resp.nouls:
        out.p_rumor = resp.nouls["rumor_real"].noul
        subj_name = resp.choices["rumor_subject"].choice
        subj = by_name.get(subj_name)
        claimed = by_name.get(proposed.rumor_about or "")
        # accept when Jev confirms the claim and agrees on (or doesn't contradict) the subject
        if out.p_rumor >= 0.6 and claimed is not None and (subj is None or subj.id == claimed.id or subj_name == "nobody"):
            out.rumor = (claimed.id, proposed.rumor_what)
    return out


@dataclass
class TravelerOutcome:
    tone: float
    believed: float = 0.0  # p(villager takes the claim as true)
    claim_present: float = 0.0
    agreed: float = 0.0  # p(villager committed to the request)
    request_present: float = 0.0
    hostile: float = 0.0  # p(the traveler was rude or threatening)


async def judge_traveler(client: AsyncTypeSafeClient, n: NPC, message: str, reply: str, proposed: dict, world) -> TravelerOutcome:
    me = n.describe(world)
    state = {
        "villager": {"name": n.name, "role": n.role, "traits": n.traits, "feels": me.get("feels"), "remembers_well": n.long_term[-5:],
                     "opinion_of_traveler": "friendly" if n.affinity.get("traveler", 0) >= 0.4 else "wary" if n.affinity.get("traveler", 0) <= -0.3 else "neutral"},
        "traveler_said": message,
        "villager_replied": reply,
    }
    qs: dict = {
        "tone": Score(
            instructions="How did the villager take what the traveler said, judging by `villager_replied`?",
            criteria=["Badly: offended, suspicious, dismissive, or angry", "Neutral: polite, noncommittal", "Well: warm, pleased, engaged, or grateful"],
        ),
        "hostile": Noul(
            instructions="`traveler_said` is rude, threatening, or insulting toward the villager or someone they care about",
            criteria={"true": "Insults, threats, mockery, or contempt", "false": "Ordinary talk, even if blunt or odd"},
        ),
    }
    if proposed.get("news"):
        state["claim"] = proposed["news"]
        qs["claim_present"] = Noul(
            instructions="`traveler_said` asserts what `claim` says (about the village or a named villager)",
            criteria={"true": "The traveler states or clearly implies it", "false": "The traveler asked about it, joked, or said something else"},
        )
        qs["believed"] = Noul(
            instructions="Given `villager` (their traits, opinion of the traveler) and `villager_replied`, the villager takes `claim` as true and would repeat it to others",
            criteria={"true": "They accept it, react to it as fact, or say they'll tell someone", "false": "They doubt it, dismiss it, ask for proof, or are too wary of a stranger to believe it"},
        )
    if proposed.get("request"):
        state["request"] = proposed["request"]
        qs["request_present"] = Noul(
            instructions="`traveler_said` asks or tells the villager to do what `request` says",
            criteria={"true": "A request, instruction, or plea for that action", "false": "Not asked, or asked of someone else"},
        )
        qs["agreed"] = Noul(
            instructions="In `villager_replied`, the villager agrees to do what `request` says",
            criteria={"true": "They say yes, will do it, or start doing it", "false": "They refuse, deflect, ask for something first, or don't answer it"},
        )
    resp = await client.system_one(state=state, questions=qs, timeout=10.0)
    out = TravelerOutcome(resp.scores["tone"].score, hostile=resp.nouls["hostile"].noul)
    if "believed" in resp.nouls:
        out.believed, out.claim_present = resp.nouls["believed"].noul, resp.nouls["claim_present"].noul
    if "agreed" in resp.nouls:
        out.agreed, out.request_present = resp.nouls["agreed"].noul, resp.nouls["request_present"].noul
    return out
