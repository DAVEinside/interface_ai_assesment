# Learning a capability on a live public website

The mock CoreServ app is the *hard* case — a frameset, layout tables, inputs with
no accessible name. This document is the other half of the argument: the same
system, unchanged, pointed at a real website on the public internet.

**The headline: adding a live site is one profile file, one policy entry and one
tenant overlay. No code changes.** That is the surface abstraction paying off —
`playwright` is imported in exactly one file, the locator vocabulary is role /
name / text / geometry, and nothing above the adapter knows what site it is on.

> **One bug did surface**, and it is worth knowing about because it only appears
> on a non-default host: the compiler stripped the caller's *default* base URL
> from the recorded entry point, so a capability recorded against any other host
> baked that host into the artifact and every later `specialize()` became a
> no-op. Fixed in `compile.py`, with two tests. It could not have been found
> against localhost alone — which is a small argument for doing this at all.

---

## Which site, and why

Two profiles ship. Both sites are published expressly so people can point testing
and automation tools at them.

| | **ParaBank** | **The Internet** |
|---|---|---|
| URL | parabank.parasoft.com | the-internet.herokuapp.com |
| What it is | Parasoft's open-source demo *bank* ([source](https://github.com/parasoft/parabank)) | Elemental Selenium's automation practice pages |
| Why it fits | A real login, accounts overview with balances, transaction search, transfers. Reads almost exactly like the brief's example goals. | `/nested_frames`, `/tables`, `/dynamic_loading`, `/status_codes` — near-perfect analogues of the CoreServ features |
| Registration | Yes, one-time, ~30 seconds | None. Credentials are printed on the page |
| Reliability | Good, but shared and periodically reset | Very high, no shared state to pollute |
| Profile | `profiles/parabank-3.0.yaml` | `profiles/the-internet.yaml` |

**Use ParaBank for the demo** — the domain match is the point.
**Use The Internet when you want a live run that cannot fail for environmental
reasons**, or when ParaBank is being slow.

### On being a good guest

Both are free, shared, public instances. A discovery run is about six page
loads; a replay is fewer. That is negligible, and this stays true only if you
keep it that way:

- `--max-steps` is your budget. Leave it at the default.
- Don't loop `demo_crystallize.py` against a public host — it does ~15 replays.
  Point it at the local CoreServ app, which is what it is written for.
- The deployment policy denies ParaBank's `/parabank/admin.htm`, which can wipe
  and re-seed the demo database **that other people are also using**. Leave that
  deny rule alone.
- Prefer read-only goals. The profile classifies `TRANSFER`, `BILL PAY` and
  `OPEN NEW ACCOUNT` as irreversible, so they will stop for approval rather than
  execute — that is the guardrail working, not something to switch off.

---

## Setup (one time)

Everything below assumes the venv is active and `.env` has your
`ANTHROPIC_API_KEY`. **No local target app is needed** — the site is the target.

> **On PowerShell.** The commands below are written for bash, so they wrap lines
> with a trailing `\`. PowerShell does not understand that and will fail with
> *"Missing expression after unary operator '--'"*. Every command in this
> document is repeated as a single-line PowerShell version in
> [the appendix](#powershell-versions) — copy from there instead.

### If you picked The Internet — nothing to do

Credentials are on the page: `tomsmith` / `SuperSecretPassword!`.

### If you picked ParaBank — register a user

Open <https://parabank.parasoft.com/parabank/register.htm> and register. Any
details will do; it is a demo database. Then put the credentials in `.env`
alongside your key:

```dotenv
PCX_SIGN_ON_PARABANK_USERNAME=yourusername
PCX_SIGN_ON_PARABANK_PASSWORD=yourpassword
```

The names are not arbitrary. A capability input `foo` is resolved from
`PCX_<CAPABILITY_ID>_FOO`, falling back to `PCX_FOO` — so these are the
variables that `sign_on_parabank`'s `username` and `password` will be found in
later, when it runs as `--setup` or as the profile's `reauth_capability` and
there is no command line to put them on. The scoping is what stops a shared
`PCX_USERNAME` from handing ParaBank's credential to The Internet's sign-on.

> Registering by hand is deliberate. Automating a sign-up on a shared public
> service is exactly the kind of thing that gets automation banned, and the
> project's own guardrails would classify it as an irreversible write anyway.

### How a credential reaches the run

`--secret name=ENV_VAR` takes the **name** of an environment variable, never the
value:

```bash
--secret username=PCX_SIGN_ON_PARABANK_USERNAME
```

Two reasons it is not `--param username="$PCX_SIGN_ON_PARABANK_USERNAME"`. The obvious one:
the shell expands that *before* this process starts, so the password lands in
your shell history and in the process list, where any other account on the
machine can read it — which is not a defensible way to handle a credential in a
project whose brief says "avoid leaking or persisting sensitive data".

The one that actually bites first: `.env` is read by **this process**, not by your
shell, so `$PCX_SIGN_ON_PARABANK_USERNAME` is empty in bash and PowerShell alike no matter
what `.env` says. Interpolating it yields an empty string, and the agent gets as
far as the login screen before reporting that it has no credentials.

Missing variables are now refused before a browser is launched, and all of them
are named at once:

```
cannot start: these inputs have no value
  --secret password=PCX_SIGN_ON_PARABANK_PASSWORD  needs $PCX_SIGN_ON_PARABANK_PASSWORD to be set

.env is read automatically; an exported shell variable takes precedence.
```

A bare `--param username` is the short form: it resolves from
`$PCX_<CAPABILITY_ID>_USERNAME` then `$PCX_USERNAME`, the same candidates the
replay engine uses to re-authenticate mid-flow. The
Internet's credentials stay plain `--param` values below, because they are
printed on its login page — they are not secrets, and pretending otherwise would
obscure where the real ones are.

---

## Step 1 — teach it to sign on

The site needs a session before anything interesting is reachable, so the first
capability is sign-on. This is the same shape as `sign_on_coreserv`.

**The Internet:**

```bash
pcx discover --id sign_on_the_internet \
  --profile the-internet \
  --tenant the_internet \
  --goal "Sign in to the secure area using the supplied credentials. Stop once the page confirms you are logged into a secure area." \
  --entrypoint "https://the-internet.herokuapp.com/login" \
  --param username=tomsmith \
  --param password='SuperSecretPassword!' \
  --output confirmation \
  --max-steps 8
```

**ParaBank:**

```bash
pcx discover --id sign_on_parabank \
  --profile parabank-3.0 \
  --tenant parabank \
  --goal "Sign in to ParaBank with the supplied customer credentials. Stop once the Accounts Overview page is displayed." \
  --entrypoint "https://parabank.parasoft.com/parabank/index.htm" \
  --secret username=PCX_SIGN_ON_PARABANK_USERNAME \
  --secret password=PCX_SIGN_ON_PARABANK_PASSWORD \
  --output signed_in_customer \
  --max-steps 10
```

This is the capability the ParaBank profile names as its `reauth_capability`.
That is a forward reference — the profile can name a capability that does not
exist yet, and this command is what brings it into being. Record it before
anything that depends on a session; if a later replay hits a session expiry and
this has not been recorded, the run fails cleanly with `SESSION_EXPIRED` and
tells you to record it.

*(PowerShell: see [the appendix](#powershell-versions).)*

You will watch it work, turn by turn — what it saw, why it chose each action,
what it cost.

## Step 2 — teach it something worth replaying

**The Internet** — read a value out of a data table that has no test ids, which
is the `row_cell` strategy earning its keep on a site it has never seen:

```bash
pcx discover --id read_table_due_amount \
  --profile the-internet \
  --tenant the_internet \
  --goal "On the sortable data tables page, find the row for the person with the last name Conway in the first table and read back their Due amount and their email address." \
  --entrypoint "https://the-internet.herokuapp.com/tables" \
  --param last_name=Conway \
  --output due_amount --output email \
  --max-steps 10
```

**ParaBank** — the direct analogue of the brief's own example goal:

```bash
pcx discover --id read_parabank_account_balance \
  --profile parabank-3.0 \
  --tenant parabank \
  --setup sign_on_parabank \
  --goal "From the Accounts Overview page, read back the account number and the current balance of the first listed account." \
  --entrypoint "https://parabank.parasoft.com/parabank/overview.htm" \
  --output account_number --output balance \
  --max-steps 12
```

## Step 3 — replay it, with no model in the loop

```bash
pcx replay read_table_due_amount --tenant the_internet --input last_name=Conway
pcx replay read_table_due_amount --tenant the_internet --input last_name=Bach
```

```bash
pcx replay read_parabank_account_balance --tenant parabank

# and the sign-on capability on its own -- no credentials on the command line,
# because a required input you omit is resolved from the environment
pcx replay sign_on_parabank --tenant parabank
```

`llm calls: 0` on both. The second Conway/Bach pair is the interesting one — one
recording, a different row, same locators.

## Step 4 — inspect what it learned

```bash
pcx show read_table_due_amount                 # the artifact
pcx show read_table_due_amount --tool-schema   # what a calling agent sees
pcx list
```

Look at the locator for the extracted cell. On a site nobody wrote a profile for
in advance, it should still resolve by *row anchor × column header* rather than
by position — the same thing it does on CoreServ.

---

## What the `--tenant` flag is doing

`--tenant` is on `discover` as well as `replay`, and on a live site you want it
on both. It does three things: supplies the host that `{{ tenant.base_url }}`
resolves to, scales every recorded wait by `timing_multiplier`, and pins the run
to that one host so a stray link cannot carry it somewhere else.

The host matters most when a run has a `--setup` capability. Setup replays a
saved sign-on to establish the session, and that sign-on has to happen on the
host this run is about to use — without a tenant it falls back to the entry
point's own origin, which is right, and with one it uses the overlay's base URL,
which is also right. What it must never do is use the *default* deployment, which
is how `--setup sign_on_parabank` ended up at `http://127.0.0.1:8799`.


The artifact stores `{{ tenant.base_url }}/tables`, not the live host. The
overlay supplies the host at run time and pins the run to it:

```yaml
# capabilities/tenants/the_internet.yaml
tenant_id: the_internet
base_url: https://the-internet.herokuapp.com
timing_multiplier: 2.0     # a public host over the internet is far slower than localhost
```

`timing_multiplier` is the one setting you will actually need to tune. Step
timeouts recorded against localhost are optimistic for anything over the public
internet; 2.0–2.5 is a sensible start, and a run that fails with
`ELEMENT_NOT_FOUND` on a step that clearly exists usually just needs more.

Because the run is pinned, a capability bound to ParaBank cannot follow a link
into The Internet — or anywhere else:

```
DENY  https://the-internet.herokuapp.com/login
      (tenant.host: host 'the-internet.herokuapp.com' is not this run's tenant)
```

---

## What to expect

These are the things most likely to need a nudge:

- **Timeouts.** See `timing_multiplier` above. Most likely thing to need tuning.
- **A detector that does not match.** Both profiles were authored from public
  pages and published source, not a full authenticated crawl. If a screen comes
  back `UNRECOGNIZED_SCREEN`, the failure detail names the route and screen
  signature, and `evidence/<run>/snapshots/` has the HTML. Fix the detector in
  the profile — no code change, and that iteration loop *is* the design.
- **ParaBank being reset.** The demo database is periodically re-seeded. If your
  registered user vanishes, register again. This is why The Internet is the
  fallback.
- **Cookie banners or interstitials.** Neither site has one today. If one
  appears, it is an `interstitials` entry in the profile — the same mechanism
  that dismisses the CoreServ maintenance notice.

## What this does and does not prove

**Does:** the surface abstraction is real, not aspirational. A completely
different live website — different markup, different domain, different error
vocabulary, HTTPS, real network latency — is configuration, not code. The
locator strategies, the outcome model, the guardrails, the tenant overlay and
the crystallization lifecycle all apply unchanged.

**Does not:** say anything about a *native desktop* surface, which remains the
largest gap between the design in REPORT §4 and what is implemented. A second
web adapter is the easy half. The Windows UIA adapter is the half that would
actually test the abstraction, and it is still unwritten.

---

---

## Adding a site of your own

Nothing above is special-cased. Both shipped sites are a profile file, a policy
entry and a tenant overlay, and a third is the same three things.

```bash
pcx profile new acme-portal --host portal.acme.example \
  --vendor Acme --product "Acme Portal" --version 4
```

That writes `profiles/acme-portal.yaml` — a commented skeleton, every section
optional — and then tells you the two things it deliberately does **not** do for
you:

1. **Add the host to `policy.yaml`.** That file is the security boundary: the
   only place a deployment grants the agent reach beyond localhost. A tool that
   edits it silently is a tool that can quietly widen its own permissions.
2. **Register a tenant** — `pcx tenant acme_portal --base-url https://... --timing 2.0`.
   The base URL the artifact's `{{ tenant.base_url }}` resolves to, and the
   factor that scales localhost-recorded timeouts for a public host.

Then just record:

```bash
pcx discover --id read_invoice_total \
  --goal "Open invoice INV-4471 and read back its total and its due date." \
  --entrypoint "https://portal.acme.example/invoices" \
  --param invoice_id=INV-4471 --output total --output due_date
```

No `--profile`. Each profile declares the `hosts:` it serves, so a run finds its
own from the entry point — `pcx profile list` shows which claims what. Pass
`--profile` only to override that.

### The profile grows; you do not write it up front

An empty profile is legal, and the first run works without one. What a profile
adds is judgement a recording cannot supply, because a recording sees each screen
once: it cannot tell an error banner from furniture, a "no such record" answer
from a failure, or a session-expiry screen from the login page it was trying to
reach. So the loop is:

1. Record a capability. It works, or it stops somewhere.
2. If a screen comes back `UNRECOGNIZED_SCREEN`, the failure detail names the
   route and screen signature, and `evidence/<run>/snapshots/` has the HTML.
3. Add the detector to the profile — an `outcomes` entry if the screen is an
   answer, `interstitials` if it is in the way, `hard_error` if the app broke.
4. `pcx recompile <trace> --id <capability>` re-crystallizes the stored trace
   with the new profile. **No second model call.**

Step 4 is the point. The expensive half is the discovery run; the profile is
cheap to iterate against a trace you have already paid for.

### And the capability grows on its own

A recorded capability starts at Type 3 and moves down as it is used — see the
gate table in [the README](../README.md). Nothing about that is per-site or
hard-coded: the ledger, the gates and the circuit breaker read the same fields
whatever application the capability was recorded against. Run
`pcx status <capability>` after a few replays and watch the counters move.


## PowerShell versions

Every command above, on one line each. PowerShell has no `\` continuation, and
pasting a multi-line block into the console is unreliable even with the backtick
`` ` ``, so these are deliberately unwrapped — copy a whole line.

One detail worth knowing: wrap the *whole* `k=v` token in double quotes when the
value contains punctuation (`--param "password=SuperSecretPassword!"`) — quoting
only the value is parsed inconsistently.

Note that nothing below interpolates a shell variable. `--secret` passes the
*name* of an environment variable, so these lines are identical in bash and
PowerShell and there is no `$NAME` / `$env:NAME` difference to get wrong.

**Step 1 — sign on**

```powershell
pcx discover --id sign_on_the_internet --profile the-internet --tenant the_internet --goal "Sign in to the secure area using the supplied credentials. Stop once the page confirms you are logged into a secure area." --entrypoint "https://the-internet.herokuapp.com/login" --param username=tomsmith --param "password=SuperSecretPassword!" --output confirmation --max-steps 8
```

```powershell
pcx discover --id sign_on_parabank --profile parabank-3.0 --tenant parabank --goal "Sign in to ParaBank with the supplied customer credentials. Stop once the Accounts Overview page is displayed." --entrypoint "https://parabank.parasoft.com/parabank/index.htm" --secret username=PCX_SIGN_ON_PARABANK_USERNAME --secret password=PCX_SIGN_ON_PARABANK_PASSWORD --output signed_in_customer --max-steps 10
```

**Step 2 — something worth replaying**

```powershell
pcx discover --id read_table_due_amount --profile the-internet --tenant the_internet --goal "On the sortable data tables page, find the row for the person with the last name Conway in the first table and read back their Due amount and their email address." --entrypoint "https://the-internet.herokuapp.com/tables" --param last_name=Conway --output due_amount --output email --max-steps 10
```

```powershell
pcx discover --id read_parabank_account_balance --profile parabank-3.0 --tenant parabank --setup sign_on_parabank --goal "From the Accounts Overview page, read back the account number and the current balance of the first listed account." --entrypoint "https://parabank.parasoft.com/parabank/overview.htm" --output account_number --output balance --max-steps 12
```

**Steps 3 and 4** are already single-line and need no change:

```powershell
pcx replay read_table_due_amount --tenant the_internet --input last_name=Conway
pcx replay read_table_due_amount --tenant the_internet --input last_name=Bach
pcx replay read_parabank_account_balance --tenant parabank

pcx show read_table_due_amount
pcx show read_table_due_amount --tool-schema
pcx list
```
