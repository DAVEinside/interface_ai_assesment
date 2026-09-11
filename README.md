# pcx — progressive crystallization for UI automation

An LLM discovers how to do something in a real application. The successful run is
compiled into a **typed, versioned capability artifact**. That artifact is then
replayed **deterministically, with no model in the decision loop** — returning
typed outputs, distinguishing expected business outcomes from real failures, and
handing control to a human when it cannot safely proceed. As a capability
accumulates evidence it is **promoted** toward zero-token determinism, and a
circuit breaker **demotes** it when it regresses.

> The model discovers. The artifact becomes a reusable capability. Deterministic
> replay is how an agent invokes it in production.

---

### An LLM learns the flow, once

![An LLM driving a legacy teller workstation, deciding one action at a time](docs/media/discovery.gif)

Six turns against a real `<frameset>`. The caption under each frame is the
model's **own** rationale, copied out of the run log — not written afterwards.
The numbered boxes are the accessibility tree it was addressing: it answers with
`e9`, and there is no CSS selector anywhere in the system.

### Then it never runs again

![The same flow replayed deterministically, with no model in the loop](docs/media/replay-success.gif)

Same flow, a member it was *not* recorded against, typed outputs — and
`llm calls: 0`. That is the whole project in one line.

### When it cannot safely proceed, a human takes the live session

![A stuck replay escalating to a human operator who finishes it by hand](docs/media/handoff.gif)

Not a ticket: the same browser, mid-flow, with the state already built up. The
operator finishes by hand and hands control back, and the engine re-checks the
checkpoint rather than assuming either outcome.

### And it earns the right to be cheap

```
── three successful replays with distinct inputs
  promotion gate -> Type 2
    PASS successful runs 3/3          PASS safety violations 0/0
    PASS action-sequence stability 1.00/0.90
    PASS human interventions 0 (must be 0)
    PASS acceptance tests present: 1
  => ELIGIBLE
[promote] promoted to Type 2
```

Every gate reported individually, passing and failing — and a circuit breaker
running the other way, so being wrong is recoverable before anyone notices.

**[The full gallery](docs/GALLERY.md)** has every scenario: both business
outcomes, the caller error that never touches the application, three recoveries,
a hard failure, and the irreversible-step guardrail with and without approval.

The target is a mock **legacy credit-union back-office application** (`target_app/`)
— a real `<frameset>`, nested layout tables, ASP.NET-style generated ids, inputs
with no `<label for>`, and injectable runtime faults (record-not-found, permission
denial, validation errors, a maintenance interstitial, session expiry, host
slowness, an unhandled application error).

Design rationale, trade-offs and limits: **[REPORT.md](REPORT.md)**.
Runs from a real discovery and real replays: **[evidence/](evidence/)**.
Pointing it at a **live public website**: **[docs/LIVE_SITE_DEMO.md](docs/LIVE_SITE_DEMO.md)**
— one profile file, one policy entry, one tenant overlay, no code changes.
Demoing the whole thing live, act by act: **[docs/WALKTHROUGH.md](docs/WALKTHROUGH.md)**.
Every scenario as a recorded run: **[docs/GALLERY.md](docs/GALLERY.md)**.

---

## Setup

**[docs/RUNNING.md](docs/RUNNING.md)** is the full walkthrough — native Windows *and* WSL
(both work equally well), VS Code setup, a verification checklist, troubleshooting.

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
pip install -e .                            # puts `pcx` on your PATH
python3 -m playwright install chromium
sudo python3 -m playwright install-deps chromium   # Linux/WSL only, not Windows
python3 scripts/doctor.py                   # checks everything and says what to run next
```

Python 3.11+. Everything runs locally; the only outbound call is to the model,
and only during discovery.

**Model access** (needed for `pcx discover` only — replay never calls a model).
Copy `.env.example` to `.env` — it is gitignored and loaded automatically:

```dotenv
ANTHROPIC_API_KEY=sk-ant-api03-...
PCX_OPERATOR_ID=TLR0042
PCX_OPERATOR_PASSWORD=openSesame!42
```

An exported shell variable takes precedence over the file. With the Claude Code
CLI installed and logged in, `PCX_LLM=cli` uses that instead of a raw key.

`pcx discover` narrates each turn live — what the model saw, what it decided and
why, what it did, and what it cost. See
[docs/RUNNING.md § Discovery](docs/RUNNING.md#discovery--the-llm-driven-half) for a sample
transcript and for how a trace becomes a capability.

Both are real model calls through the same `LLMClient` interface and the same
prompt; there is no simulated backend in this repository, because a simulated
discovery run would prove nothing. The committed evidence was produced with the
CLI backend (`claude-sonnet`).

**Credentials for the mock app** (never stored in an artifact — resolved from the
environment at run time):

```bash
export PCX_OPERATOR_ID=TLR0042
export PCX_OPERATOR_PASSWORD='openSesame!42'
```

A discovery run takes a credential by *reference* — `--secret username=PCX_MY_USER`
names an environment variable (`.env` included) rather than carrying the value, so
nothing sensitive reaches your shell history or the process list. A bare
`--param username` is the short form for `$PCX_<CAPABILITY_ID>_USERNAME`, then `$PCX_USERNAME`. Either way an unset
variable is refused before a browser starts, naming every variable it wanted.

## Running without live services

`python3 -m pytest tests/ -q` (95 tests) needs no browser, no model and no network: locator
resolution, the condition language, contract validation, the guardrails, the
redactor, tenant specialization, the compiler and the crystallization lifecycle
are all exercised against synthetic screens.

`pcx replay` needs the target app and a browser, but no model. Only
`pcx discover` needs model access.

---

## Demo path

Two terminals.

```bash
# terminal 1 — the target application
pcx target                           # http://127.0.0.1:8799
```

```bash
# terminal 2 — the whole thread
python3 scripts/demo.py              # add --replay-only to skip the model half
```

`make demo` runs, in order: two **discovery** runs (an LLM driving the live app),
the compiled artifact's **tool schema**, a **deterministic replay** with a new
input, a **business outcome**, a **contract-boundary rejection**, an injected
**application error**, a recovered **interstitial**, a mid-flow **session
expiry**, and an **irreversible step refused** without a human.

### The minimal version

```bash
# 1. discover: a goal, a live surface, an LLM in the decision loop
python3 -m pcx.cli discover \
  --id read_member_savings_balance \
  --goal "Look up member 10000001 in the CoreServ member inquiry screen and read back their current SHARE SAVINGS balance and their member name." \
  --entrypoint "http://127.0.0.1:8799/desk" \
  --setup sign_on_coreserv \
  --param member_number=10000001 \
  --output savings_balance --output member_name

# 2. replay the resulting artifact deterministically, with a different member
python3 -m pcx.cli replay read_member_savings_balance --input member_number=10000005
```

```
[replay] SUCCESS  outcome=OK
  outputs:
    savings_balance = {'amount': 3312.87, 'currency': 'USD', 'raw': '$3,312.87'}
    member_name = EUNICE P. FARRAR
    [ok  ] s1   click    e9  via role_name[0] p=0.97
    [ok  ] s2   type     e8  via label[0]     p=0.90
    [ok  ] s3   click    e9  via role_name[0] p=0.97
    [ok  ] s4   extract  e35 via label[0]     p=0.90
    [ok  ] s5   extract  e13 via label[0]     p=0.90
    [rcvr] reauth: sign_on_coreserv (re-auth success)
  llm calls: 0   degraded locators: 0   8904 ms
```

`llm calls: 0` is the point.

### The other two demos

```bash
python3 scripts/demo_handoff.py       # a replay gets stuck, a human takes over the LIVE
                                      # session, finishes by hand, hands control back
python3 scripts/demo_crystallize.py   # Type 3 -> 2 -> 1 on evidence; a regression
                                      # demotes it; clean runs bring it back
python3 scripts/demo_live_handoff.py  # the same escalation against a LIVE public site:
                                      # a recorded locator no longer resolves, a human
                                      # finishes the step, and the promotion gate then
                                      # refuses on the intervention that just happened
```

Every demo is a Python script, so Windows, macOS and Linux run the identical
code; `make` is an optional convenience alias, never a requirement.

The handoff demo starts the operator console on <http://127.0.0.1:8765> — open
it while the demo runs to watch the live screen and the control state change.

---

## Commands

| command | what it does |
|---|---|
| `pcx discover --goal … --id … [--param k=v] [--secret k=ENV] [--tenant t] [--setup c] --output name` | LLM-driven run against a live surface; emits a capability |
| `pcx replay <id> [--input k=v] [--secret k=ENV] [--tenant t] [--escalate] [--console]` | deterministic execution; returns typed outputs or a structured outcome. Required inputs you omit are resolved from `$PCX_<ID>_<NAME>` |
| `pcx invoke <id> --input k=v` | production entry point: routes to the cheapest execution type the capability has earned |
| `pcx show <id> [--tool-schema]` | print the artifact, or its JSON-Schema tool view for a calling agent |
| `pcx status <id>` | crystallization state, evidence counters, and each promotion gate with PASS/FAIL |
| `pcx promote <id> [--mark-reviewed]` / `pcx demote <id>` | apply the lifecycle by hand (it is otherwise automatic) |
| `pcx test <id>` | run the acceptance tests generated from the capability's own traces |
| `pcx recompile <trace> --id <id>` | re-crystallize a stored trace with the current compiler, without paying for the model run again |
| `pcx tenant <id> --base-url … --text-override OLD=NEW` | register a tenant overlay |
| `pcx profile new <key> --host …` / `pcx profile list` | scaffold a profile for a new site, or show which profile claims which host |
| `pcx fault <name>` | arm a fault in the mock app (test harness only — the agent's allowlist forbids `/admin`) |

Run `python3 -m pcx.cli --help`, or `pip install -e .` to get `pcx` on your PATH.

---

## Layout

```
src/pcx/
  surfaces/      the perceive/act port. model.py is the entire vocabulary the
                 layers above ever see: Role, UIElement, Observation, Action.
                 web.py is the Chromium adapter — coordinates and keystrokes only.
  perception/    ax.py builds the element inventory from the accessibility tree
                 over CDP, per frame. annotate.py draws the numbered boxes the
                 model sees and a reviewer reads.
  agent/         the discovery loop: observe -> decide -> act, with budgets,
                 policy checks and a stall detector. llm.py has two real backends.
  artifact/      schema.py (the capability), locator.py (ranked resolution
                 strategies), compile.py (trace -> artifact), profiles.py
                 (per-product knowledge), store.py (versioned YAML + ledger).
  replay/        the deterministic engine and its result contract.
  policy/        the two-layer allowlist, risk classification, redaction.
  escalation/    broker.py (who holds control) and console.py (a minimal but real
                 operator surface over the live session).
  crystallize/   promotion gates, the circuit breaker, the evidence ledger.
  evidence/      structured run logs, annotated frames, failure snapshots.

target_app/      the mock CoreServ 7.2 teller workstation
profiles/        per-product knowledge: error vocabulary, interstitials, volatile text
capabilities/    recorded artifacts (YAML, versioned) + tenant overlays + ledgers
evidence/        committed runs: discovery, replays, failures, the handoff
policy.yaml      deployment guardrails
```

## The capability artifact, briefly

```yaml
id: read_member_savings_balance
contract:
  inputs:  [{name: member_number, type: string, pattern: '\d{8}', sensitivity: pii_reference}]
  outputs: [{name: savings_balance, type: money}, {name: member_name, sensitivity: pii}]
  outcomes: [OK, MEMBER_NOT_FOUND, VALIDATION_ERROR, PERMISSION_DENIED]
  side_effect: read_only
steps:
  - id: s4
    action: extract
    bind: savings_balance
    parse: money
    target:
      strategies:                       # ranked hypotheses, most-semantic first
        - {kind: label,    role: cell, anchor_text: "SHARE SAVINGS", direction: right}
        - {kind: row_cell, role: cell, anchor_text: "SHARE SAVINGS",
                                       column_header: "CURRENT BALANCE"}
        - {kind: ordinal,  role: cell, region: workframe, index: 22}
      verify: {role: cell, text_pattern: '^-?\(?\$?[0-9][0-9,]*\.[0-9]{2}\)?$'}
```

No CSS. No XPath. No DOM ids. The balance is addressed the way a person reads it
— *the cell where the `SHARE SAVINGS` row meets the `CURRENT BALANCE` column* —
and the verifier pins the value's **shape**, never its value.

## Multi-tenant

```bash
pcx target --port 8798 --vocab customer --secret northgate   # same product, branded
                                                             # Northgate FCU
pcx replay read_member_savings_balance --tenant northgate_cu --input member_number=10000002
```

Same artifact, same digest, a seven-line overlay. It succeeds and reports drift
on every screen — which is exactly right: the flow works and the system knows
this tenant's screens differ from the recording. See REPORT §4.

## Evidence

`evidence/` contains real runs, not transcripts of intent:

- `discovery-*/` — genuine LLM-driven runs: `run.jsonl` (structured log),
  `trace.json` / `trace.full.json`, `frames/` (annotated screenshots of every
  screen the model saw), `llm/` (the model exchanges, redacted).
- `replay-transcript.md` — twelve deterministic replays covering success, both
  business outcomes, a caller error, three recovered conditions, a hard failure,
  the irreversible-step guardrail with and without approval, and the second tenant.
- `handoff-transcript.log` + `handoff-*/` — a replay that fails, escalates, is
  taken over by an operator on the live session, and completes.
- `crystallization-lifecycle.log` — Type 3 → 2 → 1 and back again.

Everything written there passes the redactor first: no SSNs, no credentials, no
raw PII. The artifacts in `capabilities/` contain no member numbers, names,
balances or operator ids at all — sensitive acceptance-test inputs live in a
gitignored `fixtures/` sidecar.

## The lifecycle, concretely

A capability is born Type 3 and moves down as evidence accumulates. Both
directions are automatic; `pcx promote` / `pcx demote` exist to force the issue.

| gate | Type 3 → 2 | Type 2 → 1 | the paper |
|---|---|---|---|
| successful runs | **3** | **5** | 10 / 50 |
| action-sequence stability | ≥ 0.90 | — | — |
| locator resolution stability | — | ≥ 0.99 | — |
| distinct input sets | — | ≥ 2 | — |
| safety violations | 0 | 0 | 0 |
| human interventions | 0 | — | — |
| acceptance tests generated | required | — | — |
| human review | — | **required** | not in the paper |

The run counts are scaled down from the paper's so the lifecycle is observable
in a demo; they are `PromotionGates` in `schema.py`, configuration rather than
design, and a capability can carry its own. Everything else is not scaled.

Two gates are deliberately *not* the paper's. Promotion to Type 1 additionally
requires a human review flag (`pcx promote --mark-reviewed`): locator resolution
is a perception problem, and "the model agreed with itself 50 times" is weaker
evidence about a UI than it is about a log line. And a run that needed a human
intervention blocks promotion outright — a flow that needed hands has not earned
fewer of them.

Promotion is applied automatically after any run that leaves a capability
eligible. `PCX_AUTO_PROMOTE=0` restores promote-by-hand. It is safe to have on
because the circuit breaker runs in the other direction on the same schedule: a
hard failure, a safety violation or an acceptance-test regression demotes and
quarantines, so being wrong is recoverable without a human noticing first.

```bash
pcx status read_member_savings_balance   # every gate, with PASS/FAIL and the counts
```

## Attribution

The crystallization lifecycle follows A. Malik, *Progressive Crystallization:
Turning Agent Exploration into Deterministic, Lower-Cost Workflows in Production*
(arXiv:2607.07052), applied to UI capabilities rather than IT-operations
playbooks. Promotion thresholds are scaled down from the paper's (10/50 runs) so
the lifecycle is observable in a demo; they are configuration, not design.
