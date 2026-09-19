"""Quick Jev example: triage a support ticket with one System One call.

Jev doesn't generate text. You send it *state* plus typed *questions*, and it
returns typed answers with probabilities. Your code owns the workflow; Jev
supplies the semantic judgments code can't make on its own.

Run:  uv run examples/triage/triage.py   (reads TYPESAFE_API_KEY from .env)
"""

from dotenv import load_dotenv
from typesafe_sdk import Choice, Noul, Score, TypeSafeClient

load_dotenv()  # loads TYPESAFE_API_KEY from ./.env

TICKET = {
    "customer": {"plan": "pro", "account_age_days": 412},
    "message": (
        "Hi, I've been trying to connect my Stripe account for 3 days and it "
        "keeps failing with a 'webhook signature mismatch' error. I'm losing "
        "sales. Please help ASAP."
    ),
}

# All three questions are independent, so they go in ONE request and run in
# parallel. Each asks a single narrow judgment.
QUESTIONS = {
    "department": Choice(
        instructions="Which team should handle the customer's `message`?",
        criteria={
            "billing": "Charges, invoices, refunds, or subscription changes",
            "technical": "Bugs, errors, or integration/setup problems",
            "sales": "Pricing, upgrades, or pre-purchase questions",
        },
    ),
    "frustration": Score(
        instructions="How frustrated does the customer sound in `message`?",
        criteria=[
            "Calm; states the problem factually",
            "Frustrated but civil; mentions impact or repeated attempts",
            "Very angry; strong language or threats to leave",
        ],
    ),
    "is_urgent": Noul(
        instructions="The `message` conveys time-sensitivity or ongoing business impact",
    ),
}


def main() -> None:
    with TypeSafeClient() as client:  # reads TYPESAFE_API_KEY, uses jev-latest
        resp = client.system_one(state=TICKET, questions=QUESTIONS)

    dept = resp.choices["department"]
    frustration = resp.scores["frustration"]
    urgent = resp.nouls["is_urgent"]

    print("--- raw answers ---")
    print(f"department : {dept.choice}  probs={ {k: round(v, 3) for k, v in dept.probabilities.items()} }  confidence={dept.confidence:.2f}")
    print(f"frustration: {frustration.score:.2f} on 0-2  confidence={frustration.confidence:.2f}")
    print(f"is_urgent  : p(yes)={urgent.noul:.3f}")
    print(f"usage      : {resp.usage.input_tokens} in / {resp.usage.output_tokens} out")

    # --- Policy lives in code, not in the model. Tune these on your own data. ---
    print("\n--- decisions ---")
    if dept.confidence < 0.4:
        print("route      : HUMAN triage (model unsure which team)")
    else:
        print(f"route      : {dept.choice} queue")

    priority = "P1" if urgent.noul > 0.8 else "P2" if urgent.noul > 0.5 else "P3"
    print(f"priority   : {priority}")

    if frustration.score >= 1.5:
        print("tone       : flag for a senior rep / empathetic template")


if __name__ == "__main__":
    main()
