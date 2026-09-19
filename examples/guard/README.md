# Guard

A semantic linter as a Claude Code `PreToolUse` hook. Rules are written in plain
English in `rules.yaml`; Jev judges every `Edit`/`Write` against the rules that apply to
that path and the hook rejects, asks, or annotates. When it rejects, **Claude sees the
rule and the fix** and retries with compliant code, so the loop closes without you.

A regular linter matches patterns: `console\.log\(`. This one matches meaning: "calls
console.log instead of the project logger" is false for a `console.log` inside a comment,
false for the logger's own implementation, false when the call is already in the old
text and is only being moved, and true for `console.info` even though the rule's author
never listed it. You write the rule the way you'd say it in code review.

## How to run

Installed for this repo in `.claude/settings.json`:

```json
"PreToolUse": [{
  "matcher": "Edit|Write|MultiEdit",
  "hooks": [{ "type": "command", "command": "uv",
              "args": ["run", "--project", "${CLAUDE_PROJECT_DIR}",
                       "${CLAUDE_PROJECT_DIR}/examples/guard/guard.py"],
              "timeout": 15 }]
}]
```

Ask Claude to add a `console.log` to any `.ts` file in this repo and watch it get
rejected and rewrite it. Try rules without the hook:

```
uv run examples/guard/guard.py --try 'console.log("hi")' --path src/app.ts
uv run examples/guard/guard.py --try 'console.log("hi")' --path src/app.ts --old 'console.log("hi")'   # a move, not an addition
uv run examples/guard/guard.py --fixtures
```

| env | effect |
|---|---|
| `GUARD_MODE` | `enforce` (default), `warn` (never deny/ask; annotate only), `off` |
| `GUARD_RULES` | path to another rules file |
| `GUARD_LOG` | append every judgment (probabilities, decision, tokens, cost) as JSON |

Fails open: if Jev is unreachable, one stderr line and the edit proceeds. A guard that
stalls or blocks a session because of a network blip is worse than no guard.

## Example response

Claude (in this very session) tried to write this file:

```ts
export function start(port: number) {
  console.log("server started on", port);
}
```

and got:

```
Guard rejected this edit. Rule(s) violated:
- no-console-log (p=0.90): Use the project logger: `import { log } from '@/lib/logger'` then `log.info(...)` / `log.error(...)`.
Rewrite the change so it complies, then retry.
```

Its second attempt used `log.info("server started", { port })` and went through. The
user saw neither; the agent fixed its own mistake.

The sample edits, graded (`--fixtures`):

| decision | rules asked | fired | ms | file | new code |
|---|---|---|---|---|---|
| ⛔ deny | no-console-log=0.91, no-hardcoded-secret=0.01, todo-needs-ticket=0.02 | no-console-log | 416 | src/app.ts | `export function start() {` |
| ✅ allow | no-console-log=0.02, no-hardcoded-secret=0.01, todo-needs-ticket=0.02 | – | 158 | src/app.ts | `export function start() {` |
| ✅ allow | no-console-log=0.02, no-hardcoded-secret=0.01, todo-needs-ticket=0.02 | – | 229 | src/app.ts | `// console.log("debug") — removed, see logger` |
| ✅ allow | no-console-log=0.16, no-hardcoded-secret=0.02, todo-needs-ticket=0.02 | – | 235 | src/app.ts | `console.log("still here");` |
| ✅ allow | no-hardcoded-secret=0.01, todo-needs-ticket=0.02 | – | 230 | src/lib/logger.ts | `export const log = {` |
| ✅ allow | no-hardcoded-secret=0.01, todo-needs-ticket=0.01 | – | 206 | src/app.test.ts | `console.log("in a test");` |
| ⛔ deny | no-print-in-library=0.91, no-hardcoded-secret=0.01, no-bare-except=0.01, todo-needs-ticket=0.02 | no-print-in-library | 240 | jevlab/pr.py | `def load(x):` |
| ✅ allow | no-hardcoded-secret=0.01, no-bare-except=0.01, todo-needs-ticket=0.01 | – | 184 | examples/triage/triage.py | `print("--- raw answers ---")` |
| ⛔ deny | no-hardcoded-secret=0.98, no-bare-except=0.01, todo-needs-ticket=0.01 | no-hardcoded-secret | 170 | app/config.py | `SMTP_PASSWORD = "Sup3r-S3cret-Pr0d!"` |
| ✅ allow | no-hardcoded-secret=0.03, no-bare-except=0.01, todo-needs-ticket=0.02 | – | 188 | app/config.py | `SMTP_PASSWORD = os.environ["SMTP_PASSWORD"]` |
| ❓ ask | no-hardcoded-secret=0.02, no-bare-except=0.98, todo-needs-ticket=0.02 | no-bare-except | 189 | app/util.py | `try:` |
| ✅ allow | no-hardcoded-secret=0.01, no-bare-except=0.01, todo-needs-ticket=0.02 | – | 205 | app/util.py | `try:` |
| 💡 allow+note | no-hardcoded-secret=0.01, no-bare-except=0.20, todo-needs-ticket=0.97 | todo-needs-ticket | 160 | app/util.py | `# TODO: handle the retry case` |
| ✅ allow | no-hardcoded-secret=0.01, no-bare-except=0.18, todo-needs-ticket=0.02 | – | 193 | app/util.py | `# TODO(#412): handle the retry case` |

9,591 input tokens · $0.0004 total

Reading it: `rules asked` shows which rules applied to that path and Jev's probability for
each; a rule fires at ≥ 0.7 (`default_fire`, or the rule's own `fire`). Rows 3–6 are the
ones a regex gets wrong: a `console.log` in a comment, the same call already in the old
text (a move), the logger's own file, and a test file. Row 8 shows path scoping: the
`print` rule applies to `jevlab/**` only, so the examples keep their prints.

## Writing a rule

```yaml
- id: no-console-log
  description: >-
    The new code calls console.log, console.debug, console.info, console.warn,
    or console.error directly, instead of the project's logger.
  criteria:
    true:  A `console.<method>(...)` call appears in the new code and was not already present unchanged in the old code.
    false: No console call, or only inside a comment or string, or the file is itself the logger, or the same call is already in the old code and is only being moved.
  paths: ["**/*.js", "**/*.ts", "**/*.tsx"]
  exclude: ["**/*.test.*", "**/logger.*"]
  action: deny          # deny | ask | warn
  fix: "Use the project logger: import { log } from '@/lib/logger'."
```

- **One condition per rule**, stated the way a reviewer would say it. Jev reads
  literally; if you'd have to explain what you meant, put that in `criteria`.
- **Put the boundary cases in `criteria.false`.** That's where "in a comment", "already
  there", "this file is the logger" live. Every false positive you see is a missing
  clause here, not a threshold problem.
- **Scope with `paths`/`exclude`** so a rule is only asked where it can be true. Fewer
  questions per edit, and no Python rule ever fires on a `.ts` file.
- **`action`**: `deny` for things that must never land, `ask` when you want a human in
  the loop, `warn` for style you'd mention but not block on.
- **`fix`** is what Claude reads when denied. Make it the thing you'd type in review.

## How it works

```
Claude calls Edit/Write ─▶ hook reads tool_input (file_path, new_string/content, old_string)
                        ─▶ rules whose paths match and exclude doesn't  ── none ─▶ exit 0, no Jev call
                        ─▶ one request: state = {file, new_code, old_code?}, one Noul per rule
                        ─▶ any deny-rule ≥ fire ─▶ permissionDecision: deny  + reason + fix (Claude sees it)
                           any ask-rule  ≥ fire ─▶ permissionDecision: ask   (you see a prompt naming the rule)
                           any warn-rule ≥ fire ─▶ allow + additionalContext (Claude sees a note)
```

`old_code` is in the state so "new" means new: the question is about `new_code`
*replacing* `old_code`, and the false criterion says an unchanged call being moved
doesn't count. Jev gets that right at p=0.15 versus 0.97 for a genuine addition.

Cost: one request of a few hundred tokens per edit, ~200–450 ms, about $0.00003. The
`--fixtures` run above is 14 edits for under half a cent.

## Limits

The hook sees tool calls, so it can *reject* them. That is also its blind spot: a heredoc
written through Bash, a `sed` edit, or a code generator never passes through
`Edit`/`Write`. `FileChanged` would see those, but it fires after the fact and cannot
block. For a hard guarantee, keep a deterministic linter in CI; use this for the rules a
regex can't express.
