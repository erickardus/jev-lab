# PII writes

Finds code that writes personal data somewhere it shouldn't. Storing a user's email in
the users table is the product; logging it, sending it to analytics, or posting it to a
CRM webhook is usually a leak. Jev reads each change and says where personal data flows;
code decides how bad that is.

## How to run

```
uv run examples/pii-writes/pii_writes.py 123 [--repo owner/name]        # a PR
uv run examples/pii-writes/pii_writes.py --diff-file changes.diff        # a diff
uv run examples/pii-writes/pii_writes.py src/auth/ src/billing/pay.py    # whole files
```

`--json` for machine output, `-v` to see every probability Jev returned (unit-level
sinks/categories, then per-line sink/sensitivity/protection). Exit code `1` when anything
is high or medium, so it drops into CI. Needs `TYPESAFE_API_KEY` in `.env`; PRs need `gh`.

## Example response

The fixture (`fixtures/signup-telemetry.diff`) is a signup handler that stores SSN and
date of birth, then leaks the email to a log line, an analytics call, Sentry, and a CRM
webhook; a checkout script that logs card numbers and pushes the email into a tag manager;
a hashed fingerprint that is fine; and a test file with fake data.

```
$ uv run examples/pii-writes/pii_writes.py --diff-file examples/pii-writes/fixtures/signup-telemetry.diff
# PII writes: signup-telemetry.diff

4 unit(s) scanned · 🔴 7 high  🟠 2 medium  🟡 1 low  ℹ️  2 info

🔴 HIGH   app/auth/signup.py:30
        log  ·  in a hunk with: email, name, gov_id, health, network, dob
           30: +    logger.info("new signup user_id=%s email=%s name=%s ip=%s", user.id, email, full_name, request.remote_addr)

🔴 HIGH   app/auth/signup.py:32
        telemetry/third_party  ·  in a hunk with: email, name, gov_id, health, network, dob
           32: +    analytics.track(user.id, "signup", {"email": email, "name": full_name, "plan": data.get("plan")})

🔴 HIGH   app/auth/signup.py:33
        telemetry/third_party  ·  in a hunk with: email, name, gov_id, health, network, dob
           33: +    sentry_sdk.set_user({"id": user.id, "email": email})

🔴 HIGH   app/auth/signup.py:35,37
        third_party  ·  in a hunk with: email, name, gov_id, health, network, dob
           35: +    requests.post(
           37: +        json={"email": email, "name": full_name, "source": "web"},

🔴 HIGH   web/src/checkout.ts:41
        log  ·  in a hunk with: email, name, financial, credentials
           41: +  console.log("submitting payment", { card: form.cardNumber, cvv: form.cvv, name: form.cardholder });

🔴 HIGH   web/src/checkout.ts:43
        telemetry/third_party  ·  in a hunk with: email, name, financial, credentials
           43: +  window.dataLayer.push({ event: "purchase", value: form.amount, email: form.email });

🔴 HIGH   web/src/checkout.ts:45
        telemetry  ·  in a hunk with: email, name, financial, credentials
           45: +    throw new Error(`payment failed for ${form.email}`);

🟠 MEDIUM app/auth/signup.py:25
        store; sensitive data (ID/financial/health/DOB/credentials)  ·  in a hunk with: email, name, gov_id, health, network, dob
           25: +    user.ssn = ssn

🟠 MEDIUM app/auth/signup.py:26
        store; sensitive data (ID/financial/health/DOB/credentials)  ·  in a hunk with: email, name, gov_id, health, network, dob
           26: +    user.date_of_birth = dob

🟡 LOW    app/auth/signup.py:42
        log; protected before write  ·  in a hunk with: email, name, gov_id, health, network, dob
           42: +    logger.debug("signup fingerprint=%s", email_hash)

ℹ️  INFO   tests/test_signup.py:2
        response/store; sensitive data (ID/financial/health/DOB/credentials); fixture data  ·  in a hunk with: email, name, gov_id
            2: +    r = client.post("/signup", json={"email": "jane.doe@example.com", "name": "Jane Doe", "ssn": "123-45-6789"})

ℹ️  INFO   tests/test_signup.py:7
        response/store; fixture data  ·  in a hunk with: email, name, gov_id
            7: +    client.post("/signup", json={"email": "jane.doe@example.com", "name": "Jane Doe"})

Severity = sink × protection × category, decided in code (see top of pii_writes.py).
Jev judges each hunk/window alone; data assembled elsewhere and written here is not visible to it.
```

~2 seconds, 7 requests, ~40k input tokens (about $0.002).

Reading it:

- **🔴 high** — unprotected personal data reaching a log, telemetry/error tracking, or a
  third party. Line 45 is a nice catch: an exception message with an email in it is a
  log/error-tracker write in disguise.
- **🟠 medium** — sensitive data (government ID, financial, health, DOB, credentials)
  going into the application's own store unprotected. Storing it is probably intended;
  storing it in plaintext is worth a look.
- **🟡 low** — the write is there but the data was hashed/masked first.
- **ℹ️ info** — test files and fixture values (`jane.doe@example.com`).
- "in a hunk with: …" lists the categories Jev saw anywhere in the surrounding hunk, as
  context; the per-line sensitivity judgment is what set the severity.

## How it works

```
units    hunks of the diff (added lines judged, removed kept as context)
         or 40-line windows of each file; lockfiles/binaries dropped in code

stage 1  one request per unit, ~18 Nouls:
           personal data is written to <sink>        x 6 sinks
           a value that is <category> is written     x 10 categories
           protected before write · fixture/placeholder data
         -> units where a sink AND a category fire go on

stage 2  one request per firing unit, per added line:
           this line writes personal data to <sink>, or is part of the
             literal payload passed to such a write   x fired sinks
           the data on this line is sensitive (ID/financial/health/DOB/credentials)
           the data on this line is hashed/masked/encrypted first
         -> code groups continuation lines of one statement into one finding

policy   severity = sink x protection x sensitivity, in code (top of pii_writes.py)
```

Sinks and their base severity: `log` high, `telemetry` high, `third_party` high, `file`
medium, `store` info, `response` info. Sensitive data bumps one level; protection drops
to low; test files and fixture data drop to info.

### Why two stages

Jev's state budget is 32k tokens and its accuracy drops with irrelevant context, so a
whole diff is never one question. Stage 1 is cheap and decides *whether* a unit matters;
stage 2 spends the tokens on exact lines only where it does. Per-line attribution matters
for more than the pointer: it is what separates `email -> log` (high) from
`ssn -> database` (medium) when both sit in the same hunk.

### Things Jev taught us while building it

- **"Builds the value the write uses" is too generous.** With that clause,
  `ssn = data.get("ssn")` fired as a store write. It reads input; it doesn't assemble a
  payload. Rewording to "is part of the literal message, payload, or object passed to
  such a write" kept `user.ssn = ssn` (which does build the stored object) and dropped
  the reads.
- **Grouping is a code problem, not a model problem.** Multi-line calls
  (`requests.post(` … `json={...}`) need to be one finding, but "adjacent firing lines"
  merged separate statements and "same sink set" split one call in two. The rule that
  works: a line continues the previous finding only if it is indented deeper, no blank
  line separates them, and the statement's first line has an unclosed bracket.
- **Half-true questions get half probabilities.** Whether a *hash* of an email is
  "personal data written to a log" is genuinely arguable, and Jev's answer sat at
  0.50–0.61 across runs. The unambiguous signal is protection (0.96). Policy now lowers
  the sink bar when protection is clear and reports the line as LOW, instead of letting
  it flicker in and out of the report.

## Limits

Jev judges a hunk or window on its own. It sees `logger.info(f"{user.email} …")`; it does
not see a payload assembled two functions away and logged here, or a helper named
`safe_log` that isn't. "Nothing found" means nothing in the changed lines, not that no
personal data leaves the program. Field names carry most of the signal, so `x = row[3]`
followed by `log(x)` is invisible to it.
