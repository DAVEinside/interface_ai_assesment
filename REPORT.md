# Design write-up

*A goal, an LLM run against a live legacy application, a typed capability
artifact, deterministic replay with real error handling, and a human who can
take the wheel — with the artifact getting cheaper and more deterministic as it
earns it.*

---

## 1. Architecture

Five layers, each with one job, and a deliberate asymmetry between them: the
layer that *perceives and acts* knows nothing about goals or capabilities, and
the layers above it know nothing about browsers.

```
  goal ──► DiscoveryLoop ──► RunTrace ──► compile ──► Capability (YAML, versioned)
             (LLM decides)   (observed)              │
                                                     ▼
  inputs ─────────────────────────────────────► ReplayEngine ──► ReplayResult
                                                 (no LLM)         outputs |
                                                     │                    business outcome |
                                                     ├── PolicyEngine     failure | escalation
                                                     ├── SessionBroker ──► operator console
                                                     └── EvidenceRecorder
                                                     │
                                                     ▼
                                            crystallize (promote / demote)
```

**Surface** (`pcx/surfaces/`) is the port. It can `observe()` — return a flat
list of `UIElement` (role, accessible name, value, rectangle, region) plus a text
digest and a screenshot — and `act()` on one of those elements. That is the
entire interface. It has no notion of a step, a capability or a goal.

**Perception is the accessibility tree, not the DOM** (`pcx/perception/ax.py`).
The target application is a `<frameset>` of nested layout tables with generated
ids (`ctl00_wf_txt3`) and inputs that have no `<label for>` — so the AX tree
reports the member-number field with an *empty* accessible name, exactly as a
real one does. Chromium exposes this over CDP (`Accessibility.getFullAXTree` per
frame, geometry from `DOM.getBoxModel`, which conveniently returns top-level page
coordinates even for nodes inside child frames). The same abstraction is what a
Windows adapter gets from UI Automation and a macOS adapter from AXUIElement.

**Action is coordinates and keystrokes.** `WebSurface` never calls
`page.click(selector)`. A click is "move the pointer to this rectangle's centre
and press"; typing is "click to focus, select-all, delete, type". The single
place a URL appears is `open()`, because every surface needs some way to express
"go to the entry point". This is the constraint that makes the artifact portable:
a UIA adapter executes the identical recorded step as "find the control, take its
bounding rectangle, click its centre".

**Trade-offs taken.**

- *Coordinates over selectors.* Slower and, on this one app today, less reliable
  than `page.click('#ctl00_wf_txt3')`. Bought: the artifact does not assume a
  DOM, so the desktop case is an adapter rather than a rewrite.
- *One flat element list, no tree.* Hierarchy is genuinely useful ("the button
  inside this dialog"), and it is the obvious thing to add. But the tree shape
  differs wildly between AX providers, whereas role/name/geometry does not — and
  geometry recovers most of what hierarchy would give (see `row_cell` below).
- *Python + Playwright + a single asyncio loop.* The operator console (FastAPI)
  and the automation share one event loop and one live browser; control transfer
  is an `asyncio.Event`, not an IPC protocol.
- *The mock target is part of the deliverable.* A public site could not produce
  a permission denial, a session expiry and an application error on demand, and
  those are the interesting half of the problem. `target_app/` is written to be
  hostile in the specific ways the real estate is hostile.

---

## 2. Artifact schema

`pcx/artifact/schema.py`. The design goal was **a tool definition with a body**,
not a macro recording: `contract` is what a calling agent needs, `steps` +
`recovery` are what the engine needs, `policy` + `crystallization` are what a
reviewer and a risk officer need.

```yaml
id: read_member_savings_balance          # + version, digest, created_by_run, model
surface:      {kind: web, app: {vendor, product, version}, entrypoint: '{{ tenant.base_url }}/desk'}
contract:
  inputs:     [{name: member_number, type: string, pattern: '\d{8}', sensitivity: pii_reference}]
  outputs:    [{name: savings_balance, type: money}, {name: member_name, sensitivity: pii}]
  outcomes:   [OK, MEMBER_NOT_FOUND, VALIDATION_ERROR, PERMISSION_DENIED]
  side_effect: read_only
steps:        [{id, intent, action, target: <Locator>, value: '{{ inputs.member_number }}',
                expect: <Condition>, expect_navigation, signature_hint, risk, retry}]
checkpoint:   {condition: <Condition>, signature_hint}
recovery:     {interstitials, transient, session_expired, reauth_capability, hard_error}
policy:       {allowed_routes, allowed_actions, denied_routes, irreversible_requires_approval}
crystallization: {execution_type, status, gates, evidence, acceptance_tests}
```

**Six decisions worth defending.**

1. **Outcomes are part of the contract, not exceptions.** `MEMBER_NOT_FOUND` is a
   value with a documented meaning and a remediation string. A caller that cannot
   tell "the system worked and the answer is no" from "the system broke" will
   either retry forever or report a wrong answer confidently. `ReplayResult.status`
   is one of `success | business_outcome | escalated | failed`, and only the last
   is a defect.

2. **A locator is a ranked list of independent hypotheses, not a path.**
   (`pcx/artifact/locator.py`.) Six strategy kinds — `role_name`, `label`,
   `row_cell`, `text`, `anchor_offset`, `ordinal` — ordered most-semantic first.
   The compiler **proves every hypothesis against the screen it was recorded on**
   and discards any that is ambiguous or resolves to a different element, so a
   locator that would have been broken on day one never gets written down. At
   replay the resolver reports *which* strategy fired; anything past the first is
   `degraded: true`, which is a drift signal the ledger consumes. The real
   examples from the recorded run:

   ```yaml
   # the member-number field: no accessible name, so adjacency is the only link
   - kind: label   role: textbox  anchor_text: "MEMBER NUMBER:"  direction: right
   # the balance: the intersection of a row and a column in a layout table
   - kind: row_cell  role: cell  anchor_text: "SHARE SAVINGS"  column_header: "CURRENT BALANCE"
   ```

3. **One condition language.** `Condition` (text/route/element/signature,
   composable with `all_of`/`any_of`/`none_of`) is used for step expectations,
   checkpoints, outcome detectors and recovery triggers. One thing to learn, one
   thing to test, one thing to render in a review UI — and it *explains itself*:
   `evaluate()` returns `(bool, why)`, which is what turns a failed checkpoint
   into `route_matches /^/desk$/ -> True; element_present -> unresolved (…)`
   rather than `AssertionError`.

4. **Nothing observed survives into the artifact.** Typed values become
   `{{ inputs.x }}`; a literal matching no declared input is a *compile error*,
   not something to bake in. The model's rationale is scrubbed before it becomes
   `intent`. Locator provenance omits the value of any control the flow reads.
   Acceptance-test inputs that are `pii_reference` or `secret` go to a gitignored
   fixture file and the artifact keeps only a reference. Three of these rules
   exist because the first compiled artifact violated them — it anchored a
   locator on `TLR0042` (an operator id the agent had just typed) and on the
   password field's row of bullet characters. Both resolved perfectly at record
   time and would have been wrong on the next invocation.

5. **The digest covers the executable body and nothing else.** Evidence counters,
   timestamps and locator provenance are excluded; `intent` is *included*,
   because the risk classifier reads it. Binding a tenant does not change the
   digest — otherwise every institution would start from a zero track record.

6. **Tenant overlay instead of a fork.** See §4.

---

## 3. Determinism & error handling

Replay (`pcx/replay/engine.py`) consults no model. Every branch it can take is
one of a fixed set, and which one it took is in the result.

**Determinism comes from three things.** Inputs are validated against the
contract before the surface is touched. Locators re-resolve semantically and are
*verified* before they are acted on — `Verify` checks role, editability, enabled
state and a name/shape pattern, and a resolution that fails verification is
discarded rather than clicked, because clicking the wrong control on a banking
screen is worse than failing. And waiting is explicit: `settle()` counts
in-flight requests across *every frame*, because `networkidle` watches only the
main frame and a frameset does almost all its navigation in a child — a bug that
showed up as "the app returned the wrong page" during a 4.5-second host stall.

**The interesting failures are runtime conditions, and they are ordered.** On
every screen, before and after every action:

| order | check | response |
|---|---|---|
| 1 | `recovery.hard_error` (`CSV-500`) | fail immediately, screenshot + DOM snapshot, never retry — the transaction state is unknown |
| 2 | declared business outcome (`MBR-404`, `SEC-403`, `AMT-114`) | return it as a value, with remediation |
| 3 | `recovery.interstitials` (`SYSTEM NOTICE`) | dismiss (bounded), re-observe, continue |
| 4 | `recovery.transient` (`PLEASE WAIT`) | wait and re-observe, never re-submit — a re-submit on a write flow double-posts |
| 5 | `recovery.session_expired` (`CSV-440`) | re-authenticate, then §below |
| 6 | otherwise | act |

Hard errors are checked before business outcomes because a crash on the
not-found screen is still a crash. Business outcomes are checked before recovery
because a definite answer beats another retry. Nothing is acted on until the
screen has been recognized. Post-action screens go through the *same* pipeline —
an interstitial usually arrives *as the response to a click*, and checking the
expectation first made every recoverable condition read as "unrecognized screen".

**Session expiry deserves its own note**, because the obvious handling is wrong.
Re-authenticating leaves the application at *its* landing screen, not where the
flow was, so resuming in place acts on the wrong screen. The engine therefore
restarts the flow — but only if `side_effect == read_only`. A flow with side
effects may have expired *after* its write committed, and a blind restart is how
an automation posts a transaction twice; those escalate instead, because only a
person can see what actually landed. Re-authentication itself is not special-cased
code: it is `recovery.reauth_capability`, replayed through the same engine, with
credentials from the environment and never from the artifact.

**Result contract.** `success` with typed outputs; `business_outcome` with a code,
description and remediation; `escalated`; or `failed` with a `FailureDetail`
naming the step, what was expected, what was observed, the route, the screen
signature, and paths to the annotated screenshot and DOM snapshot. Plus
`llm_calls` (0 on a Type 1 replay — non-zero there is a defect), degraded-locator
count, and drift signals.

**UI drift, secondarily.** Two mechanisms. Locator fallback is recorded per step,
so "this capability now resolves on strategy 3" is visible before it becomes
"this capability fails". And a screen signature (a hash of the multiset of
control roles and names) is recorded per step as `signature_hint` — compared,
reported, and *never asserted on*, for the reason in §4.

---

## 4. Heterogeneity & multi-tenant

**The surface seam.** The recorded flow speaks only role, accessible name,
visible text, and geometry. Every locator strategy is expressible over a Windows
UIA tree (`ControlType`, `Name`, `BoundingRectangle`), a macOS AX tree, or a
screen-scraped 3270 emulator where "role" is inferred from a field attribute
byte. Porting means writing `observe()`/`act()` for the new surface and extending
the role map — not touching the schema or the engine. Concretely, for a Windows
desktop app: `observe()` walks the UIA tree (`role_name` maps to `Name`,
`region` to the window/pane, `rect` to `BoundingRectangle`); `act()` is
`SetCursorPos` + `SendInput`. The two things that need real thought are the
`region` concept (frames become windows/panes, and MDI children need stable
identity) and screens with no text at all, where `label` degrades and
`anchor_offset` carries more weight than it comfortably should.

For a *legacy web* app the adapter is already the one shipped: the target
application is that case, and the strategies that carry the weight are `label`
(no `<label for>`, so adjacency is the only semantic link — and it is the same
link a human uses) and `row_cell` (layout tables with no `<th>`, addressed by
row anchor × column header).

**Multi-tenant reuse.** The unit of sharing is the **vendor product**, not the
institution:

- An **app profile** (`profiles/coreserv-7.2.yaml`) is authored once per product
  and inherited by every capability recorded against it: the error vocabulary
  (`MBR-404` → `MEMBER_NOT_FOUND`), the interstitials, session-expiry and
  hard-error detectors, the controls that are irreversible in this product, and
  the on-screen text that is *volatile* and must never be used as a locator
  anchor. This is the answer to "how does replay handle exceptional states it was
  never recorded hitting" — it does not discover them per capability; a person who
  knows the product declares them once, for every capability and every tenant.
- A **tenant overlay** (`TenantOverlay`) carries only what one institution does
  differently: base URL, vocabulary substitutions, timing multiplier, per-step
  locator overrides, disabled steps. It may not add or reorder steps — if a
  tenant needs different steps, that is a different capability and should be
  recorded as one, which keeps the blast radius of a fork visible.

This is demonstrated, not just described: `make target2` runs a second instance
of the same product branded *Northgate Federal CU* with `MEMBER` relabelled
`CUSTOMER` throughout, and the **same artifact** runs against it with a
seven-line overlay (`evidence/replay-transcript.md`, last entry). Two things had
to be fixed to make that work, and both are the general lesson:

- The vocabulary override has to reach **every** field that names on-screen text
  — including `Verify.name_pattern`. Rewriting a locator's anchor but not its
  verification pattern makes the capability find the right control and then
  reject it.
- **Screen signatures cannot be assertions.** A signature is built from control
  names, so two institutions running the same build have different signatures for
  the same screen. The portable assertions are the route, the presence of the
  controls the outputs are read from, and the fact that the screen changed at all
  (`expect_navigation`). The exact signature is kept as a hint and reported as
  drift — which is what makes it *useful*: the second-tenant run reports drift on
  every screen and still succeeds, and a spike in drift *within one tenant* means
  the application changed.

Outcome detectors survive vocabulary drift for free because they key on product
error codes (`MBR-404`), not on the sentence next to them. That is a deliberate
authoring rule for profiles, and there is a test for it.

**Detecting per-tenant drift at scale.** Every run records `(digest, tenant,
action_sequence, degraded_resolutions, drift_signals)`. Three signals order
themselves naturally: a rise in *degraded resolutions* for one tenant means that
tenant's overlay needs an entry; a rise across all tenants means the vendor
shipped a new build and the base capability needs re-recording; a change in
*action sequence* means the flow itself changed.

---

## 5. Escalation & handoff

**Detecting "stuck" is not one thing, so it is three.** During discovery: the
model emits `escalate`, or the loop detects a stall (the screen signature
unchanged while the same action repeats), three consecutive surface errors, or a
budget exhausted. During replay: a locator that will not resolve on any strategy
*and* a screen matching no declared outcome, a hard error, or an unrecognized
screen. And from the policy engine at any time: a step classified irreversible.
The third is an *approval* request rather than a handoff — "a human must decide"
and "a human must do it" are different problems, and conflating them makes both
worse — but it goes through the same queue and the same console.

**Control transfer is a single piece of state.** `SessionBroker` holds
`ControlState ∈ {AUTOMATION, PENDING_HANDOFF, HUMAN, RELEASED}` and both sides go
through it; the broker refuses any action from whichever side does not hold
control. There is one live browser session per run. Raising an intervention parks
the automation on an `asyncio.Event` — it keeps its place in the step list and
its accumulated outputs — and the operator drives *that same session*, with the
same cookies, the same half-filled form, the same server-side session. Nothing is
replayed into a fresh session.

```
AUTOMATION ──raise()──► PENDING_HANDOFF ──claim()──► HUMAN
     ▲                        │                        │
     └────────────────────────┴───release(resume)──────┘
                          release(abort) ──► RELEASED
```

The console (`pcx/escalation/console.py`) is minimal but real: it serves the live
screenshot from the running browser, forwards clicks as page coordinates and
typing as keystrokes into it, and posts resume / approve / reject / abort. On
resume the engine re-evaluates the capability's checkpoint on whatever screen the
human left behind, rather than assuming either success or failure — the run in
`evidence/handoff-transcript.log` comes back `success / OK_AFTER_HANDOFF` for
that reason. Every operator action is appended to the same evidence log with
`actor: human`, so the audit trail of a run a person finished by hand is the same
artifact as one the machine finished:

```
t= 8.256s escalation_raised   {"reason": "APPLICATION_ERROR: text_matches /CSV-500/ -> True"}
t= 8.346s handoff_claimed     {"operator": "j.reyes"}
t=  8.82s human_action        {"action": "click", "detail": "click (94,67)"}
t= 9.401s human_action        {"action": "type",  "detail": "type 8 chars"}
t=10.021s handoff_released    {"disposition": "resume", "note": "CoreServ threw CSV-500; re-ran by hand"}
t=10.326s checkpoint_after_handoff {"passed": true}
```

**What is mocked, deliberately:** the *person*. `scripts/demo_handoff.py` drives
the console's HTTP API the way a browser would when someone clicks, and finds
click coordinates by asking the surface where controls are — standing in for
someone looking at the screenshot. Everything downstream of the HTTP request is
the production path. What a real console needs beyond this is operator
authentication, a queue with routing and SLAs, a video stream instead of polled
PNGs, and a hard timeout policy; none of them change the control-transfer model.

---

## 6. Safety

**Two policy layers, intersected and never unioned.** The deployment
(`policy.yaml`) says what this installation permits; each capability carries what
that flow needs. An action is allowed only if both agree, so a capability
recorded somewhere permissive cannot gain reach by being copied into a stricter
deployment, and a deployment cannot silently widen a reviewed capability. A
capability's compiled policy is *least privilege by construction*: `allowed_actions`
is the set of verbs the recording actually used (`click, type, extract` — not
`navigate`, not `press`), and `allowed_routes` the routes it actually touched.

Checks run before every action, against the route the **surface** reports rather
than anything page content claims — and against every loaded frame, not just the
top-level document, because a frameset keeps its URL while navigating a child
frame anywhere it likes. When a run is bound to a tenant, that tenant's host is
the *only* host it may touch, which is narrower than the deployment allowlist
(which must list every tenant).

**Risk classification takes the stricter of two opinions.** A recorded step
carries the recorder's view; the deployment carries the institution's, as
patterns over control names (`POST TRANSACTION`, `\bWIRE\b`, `CLOSE ACCOUNT`) and
routes. Where they disagree the institution wins — the recorder was a language
model. The line drawn on this product: opening a sub-account is a *reversible
write* (a suffix can be closed same-day); posting to the general ledger is
*irreversible* and never executes autonomously. It raises an approval request, and
with no operator attached the run stops **before** the step with
`APPROVAL_REQUIRED` — reported as `escalated`, not as a failure, and explicitly
not a signal that trips the circuit breaker.

**Data.** Redaction is applied at the serialization boundary — every string that
reaches a log, trace, artifact or escalation payload goes through it — rather
than on the way in, because the agent has to see the screen to do the job. Built-in
categories (SSN/TIN, card numbers, credentials, bearer tokens, email, DOB) plus
per-product and per-deployment patterns, plus exact literals such as the password
just typed. Contract values are additionally masked by declared sensitivity:
`pii_reference` keeps a four-digit tail so an operator can still recognize the
record during an escalation, `pii` keeps only its length, `secret` keeps nothing.

**Limits, stated plainly.**

- Redaction is regexes over text the agent chose to capture. It will not catch a
  balance that is sensitive only in context.
- **Screenshots are the hole regex redaction cannot reach.** They are treated as a
  separate class: `capture_frames="on_failure"` is the production setting (the
  committed evidence uses `always` so a reviewer can see the runs), they are never
  embedded in an artifact, and they need their own retention policy. Pixel
  redaction is a real gap.
- The allowlist constrains *where* and *what kind*, not *how much*. Nothing stops
  an approved capability from being invoked ten thousand times; rate and volume
  limits belong at the invocation boundary and are not built.
- A capability artifact is data that a model helped produce. It is kept inert by
  construction — the template renderer does `{{ inputs.x }}` substitution only,
  with no expression evaluation, and there is a test asserting that
  `{{ __import__('os').getcwd() }}` renders as itself — but a compromised artifact
  can still direct clicks within its allowlist. Review before promotion is the
  control, which is why Type 1 requires a human review flag.
- The operator console has no authentication and binds to loopback. Fine for a
  demo, not for anything else.

---

## 7. Cuts, and what to build next

**Deliberately not built.**

- *A desktop adapter.* The abstraction is designed for it and §4 says how, but
  only the web adapter exists. This is the largest gap between the design and the
  implementation, and it is stated as such.
- *A real operator console.* Auth, queueing, routing, streaming — see §5.
- *Type 2 hybrid execution actually calling the model.* Promotion marks the steps
  that would consult it (`hybrid_steps`) and the prompt exists
  (`prompts.CLASSIFY_SYSTEM`: given an unrecognized screen and the closed list of
  outcome codes, return one — interpretation only, never an action). The engine
  does not yet call it, so Type 2 runs identically to Type 1 today. It is the
  smallest high-value thing left.
- *Learning new outcomes.* When a replay hits an unrecognized screen, the system
  has everything needed to propose a new profile outcome for review. It reports
  instead.
- *Multi-step branching.* Steps are a list. Real flows have conditionals and
  loops; the schema would need a small DAG, and `Condition` is already the guard
  language for it.
- *Concurrency and scale.* One run, one browser, one process. Sessions are already
  isolated objects, so a worker pool is mechanical rather than a redesign.
- *Sub-account opening.* The mock application implements the full write flow with
  a confirmation gate, and it is reachable — but only the read capability and a
  hand-authored guarded write were recorded, to keep the evidence focused.

**What I would build next, in order.**

1. **Type 2 for real.** Close the loop the whole project is named after: on an
   unrecognized screen, ask the model to classify it into a declared outcome
   instead of failing, at ~1k tokens instead of 30k. It converts a class of hard
   failures into business outcomes and makes the taxonomy earn its keep.
2. **A capability review UI.** The artifact is designed to be reviewable and is
   currently reviewed in a text editor. The `Condition` language and the
   per-strategy rationale exist to be rendered — with the recorded screenshot
   beside each locator, which is the one review that actually catches a bad one.
3. **The Windows UIA adapter**, against one real legacy app, to find out which of
   §4's claims survive contact.
4. **Drift telemetry across tenants.** The per-run signals are recorded; the
   aggregation that turns "capability X degrades at tenant Y" into an alert is
   not, and at hundreds of tenants that dashboard is the product.
