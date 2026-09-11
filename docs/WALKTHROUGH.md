# Live walkthrough

A demo script. Left column is what you run, indented blocks are what you say. It
runs about 25 minutes at a comfortable pace; the marked beats are the ones to cut
if you have fifteen.

Two terminals, a browser, and `.env` filled in. Before you start:

```bash
python scripts/doctor.py          # says "Everything is ready"
python -m pytest tests/ -q        # 95 passed, no browser, no model, no network
pcx target                        # terminal 1 -- leave it running
```

Open <http://127.0.0.1:8799/desk> so the audience sees the target before you
automate it.

---

## Act 0 — The target, and why it is the hard case

> This is CoreServ 7.2, a teller workstation. It is a mock, and it is mock in
> exactly the ways that make this problem hard. It is a real `<frameset>`.
> The layout is nested tables. The ids are ASP.NET-generated —
> `ctl00$ContentPlaceHolder1$txtMbrNo`. The inputs have no `<label for>`. There
> is no API and there will never be one.
>
> That is the environment this is for. Not a modern SPA with test ids — the
> system that already runs the business and cannot be changed.

Show the DOM in devtools for five seconds. That is the whole motivation.

> One more thing: this app can be told to misbehave. Record-not-found, permission
> denial, a maintenance banner, session expiry, a 4.5-second stall, an unhandled
> 500. We will use all of them.

---

## Act 1 — Perception: how it sees the screen

**Cut if short on time, but it is the foundation for everything after.**

> Before anything can act, it has to perceive. Two things are deliberately
> absent from this whole system: CSS selectors and XPath.
>
> Perception is the accessibility tree, pulled over the Chrome DevTools Protocol,
> per frame, plus geometry. What comes back is a list of *controls*: a role, an
> accessible name, a bounding box, whether it is editable. That is what a screen
> reader gets, and it is what a person gets.

Open any `evidence/discovery-*/frames/*.png`.

> Every screen the model saw, with numbered boxes drawn on the elements it could
> address. The model sees the screenshot and the inventory together, and it
> answers with a number — "click e9". It never writes a selector, because the
> vocabulary it has been given does not contain one.
>
> Actions are coordinates and keystrokes. `page.mouse.click(x, y)`.
> `keyboard.type(...)`. Nothing else. That is the whole reason this generalizes
> past the browser: the port is *perceive* and *act*, and a browser is one
> implementation of it. `playwright` is imported in exactly one file.

---

## Act 2 — Discovery: a goal, a live surface, a model in the loop

```bash
pcx discover \
  --id read_member_savings_balance \
  --goal "Look up member 10000001 in the CoreServ member inquiry screen and read back their current SHARE SAVINGS balance and their member name." \
  --entrypoint "http://127.0.0.1:8799/desk" \
  --setup sign_on_coreserv \
  --param member_number=10000001 \
  --output savings_balance --output member_name
```

> A goal in English. An entry point. The inputs it may use and the outputs it
> must capture.
>
> `--setup sign_on_coreserv` is worth pausing on. Signing in is already a
> capability, so the precondition is established by *replaying* it —
> deterministically, no model — rather than teaching the model to log in again
> every time. Capabilities compose from the first command.

Let it run. It narrates every turn.

```
step 1  see    CoreServ 7.2 - Teller Workstation  17 elements
        think  I need to navigate to the MEMBER INQUIRY function to look up
               member 10000001, so I'll click that link.
        act    click @e9
step 2  see    19 elements
        think  I need to type the member number into the member number field.
        act    type @e8 text='10000001'
```

> That is a real model call, live, deciding one action at a time from what is on
> screen. What you are watching is the expensive part — and the part we are about
> to stop paying for.

---

## Act 3 — The artifact: what the run compiles into

```bash
pcx show read_member_savings_balance
```

> The run is over. What survives is not the transcript — the transcript is
> deliberately *not* in the artifact. What survives is a typed, versioned
> description of the flow.

Scroll to the extract step.

```yaml
- id: s4
  action: extract
  bind: savings_balance
  parse: money
  target:
    description: cell located by label from 'SHARE SAVINGS'
    strategies:
      - {kind: label,    role: cell, anchor_text: "SHARE SAVINGS",   direction: right}
      - {kind: label,    role: cell, anchor_text: "CURRENT BALANCE", direction: below}
      - {kind: row_cell, role: cell, anchor_text: "SHARE SAVINGS",
                                     column_header: "CURRENT BALANCE"}
      - {kind: ordinal,  role: cell, region: workframe, index: 22,
         note: "Positional fallback. If this is the strategy that fires, the
                screen has changed and the capability should be reviewed."}
    verify: {role: cell, text_pattern: '^-?\(?\$?[0-9][0-9,]*\.[0-9]{2}\)?$'}
```

> Five ranked hypotheses about how to find one number, most semantic first. The
> balance is addressed the way a person reads it: *the cell where the SHARE
> SAVINGS row meets the CURRENT BALANCE column*.
>
> Two details that matter more than they look.
>
> The compiler **proved** each of these against the recorded screen before
> writing it down. A strategy that did not resolve to the same element was
> discarded, not guessed at.
>
> And the verifier pins the value's *shape*, never its value. It checks that what
> it found looks like money. Bake in `$4,182.55` and the capability works exactly
> once.
>
> Last strategy is positional, and it carries its own warning. If `ordinal` is
> the one that fires, that is recorded as a drift signal — the flow still worked,
> and the system now knows the screen moved.

```bash
pcx show read_member_savings_balance --tool-schema
```

```json
{
  "name": "read_member_savings_balance",
  "description": "Look up member {{ inputs.member_number }} in the CoreServ member inquiry screen and read back their current SHARE SAVINGS balance and their member name.",
  "input_schema": {
    "type": "object",
    "properties": {
      "member_number": {"type": "string", "pattern": "\\d{8}"}
    },
    "required": ["member_number"]
  }
}
```

> Same artifact, viewed as a tool definition. That is the thesis in one screen: a
> capability is **a tool definition with a body**. An agent calls it the way it
> calls any other tool, and what executes is the recorded flow.

---

## Act 4 — Replay: the same flow, no model

```bash
pcx replay read_member_savings_balance --input member_number=10000005
```

Note the **different member number**.

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
  llm calls: 0   degraded locators: 0   8904 ms
```

> `llm calls: 0` is the point of the whole project.
>
> A different member than the one it was recorded against — so this is a
> parameterized flow, not a recording of one member's data. `via label[0]` on
> every step means the *primary* strategy resolved; nothing fell back.
>
> And the outputs are typed. `savings_balance` is money, parsed into an amount
> and a currency, not a string that happens to have a dollar sign in it.

---

## Act 5 — What "failure" actually means

Four runs, side by side. This is where the design earns its keep.

```bash
pcx replay read_member_savings_balance --input member_number=99999999   # no such member
pcx replay read_member_savings_balance --input member_number=10000003   # advisory hold
pcx replay read_member_savings_balance --input member_number=1000000    # caller error: 7 digits
pcx fault app_error
pcx replay read_member_savings_balance --input member_number=10000001   # injected 500
```

| input | status | outcome | what the caller should do |
|---|---|---|---|
| `99999999` | `BUSINESS_OUTCOME` | `MEMBER_NOT_FOUND` | Confirm the number with the caller |
| `10000003` | `BUSINESS_OUTCOME` | `PERMISSION_DENIED` | Route to a supervisor; do not retry |
| `1000000` | `FAILED` | `INVALID_INPUT` | Fix the input — **the app was never touched** |
| injected 500 | `FAILED` | `APPLICATION_ERROR` | Real failure. Screenshot and DOM snapshot saved |

> Most automation has two states: it worked, or it threw. That is not enough to
> put in front of a caller.
>
> "No such member" is not a failure. It is the *system working* — a definite,
> correct answer to a lookup. It comes back as a declared business outcome with a
> code, a description and a remediation, and it is a **contract value**, not an
> exception.
>
> A malformed member number is different again. Look at the timing on that one:

```
[replay] FAILED  outcome=INVALID_INPUT
  input 'member_number' does not match /\d{8}/
  hint    : Rejected at the contract boundary -- the application was never touched.
  llm calls: 0   degraded locators: 0   1 ms
```

> One millisecond. No browser opened. In a regulated system, the difference
> between "we rejected your input" and "we drove a session into the core banking
> platform with your bad input" is the difference between a log line and an
> incident.

---

## Act 6 — Recovery: three things that would break a script

```bash
pcx fault interstitial    && pcx replay read_member_savings_balance --input member_number=10000002
pcx fault slow            && pcx replay read_member_savings_balance --input member_number=10000001
pcx fault session_timeout && pcx replay read_member_savings_balance --input member_number=10000001
```

All three come back `SUCCESS  outcome=OK`. The interesting line is in the third:

```
    [rcvr] reauth: sign_on_coreserv (re-auth success)
```

> A maintenance banner, a four-and-a-half second stall, and a session that
> expired halfway through. A recorded macro dies on all three.
>
> The third is the one to look at. The session expired mid-flow, and the recovery
> was to **replay the sign-on capability** — composition, not a login routine
> hard-coded into the engine. Then, because this flow is read-only, it restarted
> from the top. A flow with side effects would escalate instead, because
> re-running half a transfer is not a recovery.
>
> None of this is the model. Each is a declared rule in the application profile.

---

## Act 7 — Policy and safety

```bash
pcx replay post_transaction_guarded --input account=10000001-0000 --input amount=250.00
```

```
[replay] ESCALATED  outcome=APPROVAL_REQUIRED
  failure : APPROVAL_REQUIRED at s4 (Commit the posting to the core ledger)
    observed: step 's4' is classified irreversible ('POST TRANSACTION button')
              and requires human approval
    hint    : The run stopped before the step, not during it.
```

```bash
pcx replay post_transaction_guarded --input account=10000001-0000 --input amount=250.00 --approve
```

```
[replay] SUCCESS  outcome=OK
```

> Same capability, same inputs. The only difference is a human said yes.
>
> "The run stopped **before** the step, not during it" is the line that matters
> to a bank. It did not post and then ask.

Show `policy.yaml`.

> Guardrails are two layers, **intersected, never unioned**. A capability can be
> narrower than the deployment allows; it can never be wider.
>
> The action vocabulary has no file upload, no download, no script execution — so
> a malformed artifact or a confused model cannot reach them, because the words
> do not exist.
>
> Irreversibility is decided by the *stricter* of two views. The recording was
> made by a language model; this list was written by a person.

```
ALLOW  parabank.parasoft.com/parabank/overview.htm
DENY   parabank.parasoft.com/parabank/admin.htm     denied_routes
DENY   evil.example.com/x                            deployment.allowed_hosts
DENY   the-internet.herokuapp.com/login              tenant.host  [tenant=parabank]
```

> That last one is tenancy. A run bound to one institution cannot follow a link
> into another's instance — not by policy review, by construction.
>
> And on data: everything written to evidence passes a redactor first. No SSNs,
> no card numbers, no credentials. The artifacts contain no member numbers, no
> names, no balances at all — sensitive acceptance-test inputs live in a
> gitignored sidecar. Screenshots of a banking screen are regulated data, so in
> production the capture setting is `on_failure`, not `always`.

---

## Act 8 — Escalation: a human on the live session

```bash
python scripts/demo_handoff.py
```

Open <http://127.0.0.1:8765> as it runs.

> A replay hits something it cannot recover from. It does not guess, and it does
> not die. It raises an intervention and parks.
>
> What the operator gets is not a ticket. It is **the live session** — the same
> browser, mid-flow, with the state already built up. They take control, finish
> the step by hand, and hand it back. The engine then re-evaluates the checkpoint
> rather than assuming either outcome, and returns `OK_AFTER_HANDOFF`.
>
> Control is a state machine: AUTOMATION → PENDING_HANDOFF → HUMAN → back. While
> a human holds it, the automation is refused. Two things never drive one browser.

---

## Act 9 — Crystallization: earning the right to be cheap

```bash
python scripts/demo_crystallize.py
```

> This is the paper this is built on — Malik, *Progressive Crystallization* —
> applied to UI capabilities rather than IT-operations playbooks.

| | Type 3 | Type 2 | Type 1 |
|---|---|---|---|
| how it runs | an LLM decides every action | fixed steps; the model only *interprets* an unrecognized screen | fixed steps, no model |
| model calls | one per step | rare | **zero** |
| when | just discovered | earning trust | earned |

> Everything starts at Type 3 and moves down **on evidence**, never on a date.

```
── starting point: freshly discovered, Type 3
  promotion gate -> Type 2
    FAIL successful runs 0/3

── three successful replays with distinct inputs
  promotion gate -> Type 2
    PASS successful runs 3/3
    PASS action-sequence stability 1.00/0.90
    PASS safety violations 0/0
    PASS human interventions 0 (must be 0)
    PASS acceptance tests present: 1
  => ELIGIBLE
[promote] promoted to Type 2
```

The gates:

| gate | T3 → T2 | T2 → T1 | the paper |
|---|---|---|---|
| successful runs | **3** | **5** | 10 / 50 |
| action-sequence stability | ≥ 0.90 | — | — |
| locator resolution stability | — | ≥ 0.99 | — |
| distinct input sets | — | ≥ 2 | — |
| safety violations | 0 | 0 | 0 |
| human interventions | 0 | — | — |
| acceptance tests generated | required | — | — |
| human review | — | **required** | not in the paper |

> Only the run counts are scaled down, so the lifecycle is watchable in a demo —
> they are configuration, not design. Everything else is not scaled.
>
> Two gates are deliberately *stricter* than the paper. Type 1 needs a human
> review flag, because locator resolution is a perception problem and "the model
> agreed with itself fifty times" is weaker evidence about a UI than about a log
> line. And a run that needed a human intervention blocks promotion outright — a
> flow that needed hands has not earned fewer of them.

Watch the refusal in the log:

```
[promote] refused: not eligible for Type 1: PASS successful runs 8/5;
          PASS locator resolution stability 1.00/0.99; PASS distinct input sets 4/2;
          PASS safety violations 0/0; FAIL human review: PENDING
```

> Every gate reported individually, passing and failing. Not a yes/no.
>
> Then the demo breaks it, and the circuit breaker runs the other way: a hard
> failure demotes it one level and quarantines it, so the *next* call is served by
> a more capable, more expensive execution type instead of failing again. Clean
> runs bring it back — but recovery is judged only on what happened *since* the
> demotion, or the failure that caused it would block recovery forever.
>
> That breaker is what makes automatic promotion safe to have at all. Being wrong
> is recoverable before a human notices.

```bash
pcx invoke read_member_savings_balance --input member_number=10000001
```

> And this is the production entry point. The caller does not choose an execution
> type — it routes to the cheapest one the capability has earned. That is the
> whole economic argument: the same call gets cheaper as the system learns it is
> safe, without the caller changing anything.

---

## Act 10 — The same artifact, a second institution

```bash
pcx target --port 8798 --vocab customer --secret northgate    # terminal 3
pcx replay read_member_savings_balance --tenant northgate_cu --input member_number=10000002
```

> Same product, different institution. Different host, and the screens say
> CUSTOMER where the recording said MEMBER.
>
> Same artifact. **Same digest.** A seven-line overlay supplies the host, the
> vocabulary and a timing multiplier. It succeeds — and reports drift on every
> screen, which is exactly right: the flow works, and the system knows this
> tenant's screens differ from the recording.
>
> The digest staying the same is load-bearing. One artifact, one track record,
> many tenants. Bind it and the identity does not change, so evidence from every
> tenant counts toward the same capability.

---

## Act 11 — A live public website

Everything so far was localhost. Now the same system, unchanged, on the internet.

```bash
pcx replay sign_on_parabank --tenant parabank
pcx replay read_parabank_account_balance --tenant parabank
```

> This is ParaBank — Parasoft's open-source demo bank, published for pointing
> testing tools at. A real login, a real accounts overview, real network latency,
> HTTPS, a host nobody here controls.
>
> Adding it was **one profile file, one policy entry, one tenant overlay. No code
> changes.** That is the surface abstraction paying off rather than being
> asserted.
>
> No credentials on the command line, either. A required input you omit is
> resolved from the environment — the same variables re-authentication uses.

Then the live escalation:

```bash
python scripts/demo_live_handoff.py
```

> One recorded locator is overridden with one that matches nothing — standing in
> for the site having moved a control since the recording. Every ranked strategy
> misses, the retries are spent, and it escalates rather than clicking whatever
> is nearby. A human finishes it on the live page, hands back, and the run
> completes as `OK_AFTER_HANDOFF` — with `human_interventions: 1` on the ledger.
>
> Then `pcx status` refuses to promote it, on exactly that count.

**One honest note, and it is worth telling:** pointing this at a live site found a
real bug that localhost could never surface. The compiler stripped the caller's
*default* base URL from the recorded entry point, so a capability recorded against
any other host baked that host into the artifact, and every later tenant binding
silently became a no-op. Fixed, with two tests.

---

## Act 12 — Any site, including one nobody has seen

```bash
pcx profile new acme-portal --host portal.acme.example --vendor Acme --product "Acme Portal"
```

> Nothing in the last two acts was special-cased. A third site is the same three
> things, and this scaffolds the first of them.
>
> It prints the two it deliberately does *not* do for you: adding the host to
> `policy.yaml`, which is the security boundary and not a file a tool should widen
> on its own, and registering the tenant.
>
> Then you just record. No `--profile` flag — each profile declares the hosts it
> serves and a run finds its own from the entry point.

```bash
pcx profile list
```

```
meridian data systems/coreserv teller workstation@7.2   127.0.0.1:8799, 127.0.0.1:8798, localhost
parasoft/parabank@3.0                                   parabank.parasoft.com
elemental selenium/the internet@current                 the-internet.herokuapp.com
```

> And the profile is not something you write up front. An empty one is legal and
> the first run works without it. When a screen comes back `UNRECOGNIZED_SCREEN`,
> the failure names the route and the signature, the HTML is in the evidence
> directory, you add the detector — and then `pcx recompile` re-crystallizes the
> **stored trace** against the new profile, with no second model call.
>
> The expensive half is the discovery run. Iterating the knowledge is free.

---

## Closing — the through-line, and the limits

> The model discovers. The artifact becomes a reusable capability. Deterministic
> replay is how an agent invokes it in production.
>
> Every requirement runs through one thread: a goal in English, an LLM-driven run
> that completes it, a saved artifact, deterministic replay with typed inputs and
> outputs and real outcome handling, a human escalation path onto the live
> session, and evidence for both halves — on localhost and on the public
> internet.

Be straight about what is not done:

> The largest gap is that "computer use" here means one surface. The port is
> genuinely general — perceive and act, roles and names and geometry — and a
> second *web* adapter would be the easy half. A Windows UIA adapter is the half
> that would actually test the abstraction, and it is unwritten.
>
> The drift signal is noisier on a real site than on the mock, because a screen's
> identity is computed from its controls, and on a real page some controls are
> named after data.
>
> And promotion thresholds at 3 and 5 runs are demo scale. In production they are
> the paper's 10 and 50, and that is a config change, not a design change.

```bash
python -m pytest tests/ -q     # 95 passed
```

> Ninety-five tests, no browser, no model, no network. The locator strategies,
> the condition language, the contract boundary, the guardrails, the redactor,
> tenant binding, the compiler and the whole crystallization lifecycle, all
> against synthetic screens. The evidence directory has the runs.
