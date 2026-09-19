# Support-ticket triage

The smallest possible Jev example: one request, three questions, one state.

## How to run

```
uv run examples/triage/triage.py
```

Needs `TYPESAFE_API_KEY` in `.env`. No PR or network access beyond the TypeSafe API.

## Example response

```
$ uv run examples/triage/triage.py
--- raw answers ---
department : technical  probs={'sales': 0.0, 'technical': 1.0, 'billing': 0.0}  confidence=1.00
frustration: 1.00 on 0-2  confidence=1.00
is_urgent  : p(yes)=0.990
usage      : 510 in / 73 out

--- decisions ---
route      : technical queue
priority   : P1
```

## What it shows

The three primitives side by side on the same ticket:

| question | primitive | answer |
|---|---|---|
| Which team handles this? | **Choice** | one option + a probability per option + confidence |
| How frustrated is the customer? | **Score** | a position on ordered levels (0–2) + confidence |
| Is it urgent? | **Noul** | p(yes) — no separate confidence, the probability *is* the answer |

The three questions are independent, so they go in one request and run in parallel.
Everything under `--- decisions ---` is policy in code: the thresholds (`> 0.8` → P1,
`confidence < 0.4` → human triage) are yours to tune, and changing them doesn't require
asking Jev again.
