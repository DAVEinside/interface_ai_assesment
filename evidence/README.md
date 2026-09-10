# Evidence

Real runs against the mock CoreServ 7.2 application, produced by the code in this
repository. Regenerate with `bash scripts/make_evidence.sh` (the discovery runs
are preserved rather than re-run — they are genuine model-driven runs, and a
re-run produces a different, equally valid trace).

## Discovery — an LLM driving a live surface

| directory | goal | model | steps |
|---|---|---|---|
| `discovery-20260909T034502-6b08/` | sign on to the teller workstation | claude-sonnet | 5 |
| `discovery-20260909T034714-4663/` | look up a member and read the SHARE SAVINGS balance | claude-sonnet | 6 |

Each contains:

- `run.jsonl` — the structured, redacted event log: every observation (route,
  title, screen signature, element count), every decision (the model's one-line
  rationale, the action, latency, token usage), every policy check, every action
  result.
- `trace.json` — the observed record the compiler reads. Note what is *not* in
  it: the model transcript. The artifact is decoupled from the transcript by
  construction.
- `trace.full.json` — the same plus each step's perceived element inventory, so
  the capability can be re-crystallized later with an improved compiler
  (`pcx recompile`) without paying for the model run again.
- `frames/` — an annotated screenshot of every screen the model saw, with the
  numbered element boxes it was addressing.
- `llm/` — the request/response pairs, redacted.
- `manifest.json` — the run summary.

## Replay — the production path, no model

`replay-transcript.md` is the readable index; each entry names its own
`replay-*/` directory. Twelve runs:

| # | scenario | result |
|---|---|---|
| 1 | happy path | `SUCCESS` + typed outputs |
| 2 | a different member, same artifact | `SUCCESS` |
| 3 | member not on file | `BUSINESS_OUTCOME MEMBER_NOT_FOUND` |
| 4 | advisory hold on the record | `BUSINESS_OUTCOME PERMISSION_DENIED` |
| 5 | seven-digit member number | `FAILED INVALID_INPUT` — rejected at the contract boundary, app never touched |
| 6 | injected maintenance interstitial | `SUCCESS`, recovery recorded |
| 7 | injected 4.5s host stall | `SUCCESS`, recovery recorded |
| 8 | injected mid-flow session expiry | `SUCCESS` — re-auth by capability composition, read-only flow restarted |
| 9 | injected `CSV-500` | `FAILED APPLICATION_ERROR` + screenshot + DOM snapshot |
| 10 | irreversible step, no operator | `ESCALATED APPROVAL_REQUIRED`, stopped *before* the step |
| 11 | the same step, human pre-approved | `SUCCESS` |
| 12 | second tenant, `CUSTOMER` vocabulary | `SUCCESS` + drift on every screen |

Every one reports `llm calls: 0`.

## Escalation and handoff

`handoff-transcript.log` and `handoff-*/`. A replay hits `CSV-500`, escalates,
an operator claims it, clicks and types on **the same live browser session**,
releases control, and the engine re-evaluates the checkpoint: `OK_AFTER_HANDOFF`.
The operator's actions are in the same `run.jsonl` with `actor: human`.

## Crystallization

`crystallization-lifecycle.log`. A discovered capability at Type 3 earns
promotion to Type 2 after three clean runs, to Type 1 after eight plus a human
review flag, is demoted to Type 2 and quarantined by the circuit breaker when the
application throws `CSV-500`, and returns to candidacy after three clean runs.

## A note on screenshots

Frames are captured for every step here (`PCX_FRAMES=always`) so a reviewer can
see the runs. The production default is `on_failure`: screenshots of a banking
screen are regulated data that regex redaction cannot reach, so they are held in
memory and written only when a run fails. See REPORT §6.
