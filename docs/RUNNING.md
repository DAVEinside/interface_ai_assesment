# Running and verifying pcx — Windows, WSL, macOS, Linux

**Both work. Pick either.** Native Windows and WSL run this project equally well;
there is nothing in the code that prefers one. Choose on which environment you
would rather live in day to day.

### First, a correction about that "10× slower"

WSL is **not** 10× slower. *Crossing the Windows↔Linux filesystem boundary* is.
If you clone into `~/projects/pcx` (inside the Linux filesystem), WSL runs this at
full native speed — for a Python + Chromium workload, generally a little faster
than Windows. The penalty applies only if you put the repo on `/mnt/c/Users/...`
and make Linux read Windows files through a translation layer on every access.

That is one `cd`, not a platform tax. So it is not a reason to avoid WSL — but it
is also not a reason to use it if you would rather stay native.

| | Native Windows | WSL |
|---|---|---|
| Speed | fast | fast (repo inside `~`, not `/mnt/c`) |
| Setup steps | fewer — Chromium needs no extra libraries | one extra: `install-deps` |
| `make` | not available; run the Python commands instead | works |
| Shell syntax | `$env:VAR = "x"` (PowerShell) | `export VAR=x` |
| Seeing the browser (`PCX_HEADLESS=0`) | just works | works on Win 11 (WSLg); needs an X server on Win 10 |
| Debugging in VS Code | native, no remote hop | via the WSL extension |

**Verified honestly:** every command below was executed on Linux. The Windows path
is the same Python with no POSIX dependencies left in it — the demo scripts are
Python, not bash, and nothing needs `PYTHONPATH` — but I could not execute it on
Windows myself. If anything there misbehaves, it will be in §Troubleshooting, and
WSL is a working fallback.

---

# Path A — native Windows + VS Code

## A1. Install

Python **3.11 or newer** is required. Check in PowerShell:

```powershell
python --version
```

If it is missing or older, install from [python.org](https://www.python.org/downloads/)
(tick **Add python.exe to PATH**) or `winget install Python.Python.3.12`.

Open the project folder in VS Code, then a terminal (`Ctrl+~`, PowerShell):

```powershell
cd C:\path\to\pcx
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
pip install -e .
python -m playwright install chromium
```

If `Activate.ps1` is blocked by execution policy:

```powershell
Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned
```

There is **no `install-deps` step on Windows** — that is Linux-only. The Chromium
download is self-contained.

In VS Code: `Ctrl+Shift+P` → **Python: Select Interpreter** → `.\.venv\Scripts\python.exe`.

## A2. Environment

```powershell
$env:PCX_OPERATOR_ID = "TLR0042"
$env:PCX_OPERATOR_PASSWORD = "openSesame!42"
```

Those last for the current terminal. To avoid retyping them, add both lines to
your PowerShell profile (`notepad $PROFILE`), or let the demo scripts default
them — `scripts/demo.py` and `scripts/demo_crystallize.py` both set these two
values if they are unset.

In **cmd.exe** instead: `set PCX_OPERATOR_ID=TLR0042`.

## A3. Verify, then test

```powershell
python scripts\doctor.py
python -m pytest tests\ -q
```

Expect `Everything is ready` (warnings before the app is running are fine) and
**93 passed**.

## A4. Run it

**Terminal 1** — the target application, left running:

```powershell
.\.venv\Scripts\Activate.ps1
pcx target
```

Open <http://127.0.0.1:8799> and sign in as `TLR0042` / `openSesame!42`.

**Terminal 2** — everything else:

```powershell
.\.venv\Scripts\Activate.ps1
$env:PCX_OPERATOR_ID = "TLR0042"; $env:PCX_OPERATOR_PASSWORD = "openSesame!42"

pcx replay read_member_savings_balance --input member_number=10000001
python scripts\demo.py --replay-only      # the full tour, no model needed
```

**Terminal 3** — the second tenant, when you want it:

```powershell
pcx target --port 8798 --vocab customer --secret northgate
```

`make` does not exist on Windows and is not needed — every `make` target is a
one-line alias for a Python command:

| `make …` | Windows equivalent |
|---|---|
| `make test` | `python -m pytest tests\ -q` |
| `make doctor` | `python scripts\doctor.py` |
| `make target` | `pcx target` |
| `make target2` | `pcx target --port 8798 --vocab customer --secret northgate` |
| `make demo` | `python scripts\demo.py` |
| `make demo-handoff` | `python scripts\demo_handoff.py` |
| `make demo-crystallize` | `python scripts\demo_crystallize.py` |

Now skip to **§ Common ground** below — everything from there is identical on
both platforms.

---

# Path B — WSL + VS Code

## B1. Set up WSL

Install the **WSL** extension (`ms-vscode-remote.remote-wsl`), then
`Ctrl+Shift+P` → **WSL: Connect to WSL**. The bottom-left should read
`WSL: Ubuntu`, and every terminal you open is now a Linux shell.

> **Clone into `~`, not `/mnt/c/`.** This is the whole of the performance advice.

```bash
mkdir -p ~/projects && cd ~/projects
# unzip the archive here, or: git clone <your-repo-url> pcx
cd pcx && code .
```

Python 3.11+ (`python3 --version`); on Ubuntu 22.04:

```bash
sudo add-apt-repository -y ppa:deadsnakes/ppa && sudo apt update
sudo apt install -y python3.11 python3.11-venv
```

## B2. Install

```bash
cd ~/projects/pcx
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
pip install -e .
python -m playwright install chromium
sudo python -m playwright install-deps chromium     # the step people miss
```

If `sudo` inside the venv complains, use `sudo $(which python) -m playwright install-deps chromium`,
or install the libraries directly:

```bash
sudo apt install -y libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
  libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 \
  libgbm1 libpango-1.0-0 libcairo2 libasound2
```

VS Code: `Ctrl+Shift+P` → **Python: Select Interpreter** → `./.venv/bin/python`.

## B3. Verify, test, run

```bash
python scripts/doctor.py
python -m pytest tests/ -q                 # 93 passed

export PCX_OPERATOR_ID=TLR0042
export PCX_OPERATOR_PASSWORD='openSesame!42'    # single quotes: ! is history expansion

pcx target                                  # terminal 1
make target2                                # terminal 3, when you want it
```

WSL2 forwards localhost, so <http://127.0.0.1:8799> and the operator console at
<http://127.0.0.1:8765> open in your normal Windows browser.

---

# Common ground

Everything below is identical on both paths. Use `/` or `\` as your shell prefers.

## Replay — the production path, no model needed

The fastest way to see the system work, and it needs **no API key**.

```bash
pcx list                                             # what is recorded
pcx show read_member_savings_balance --tool-schema   # what a calling agent sees
pcx replay read_member_savings_balance --input member_number=10000001
```

```
[replay] SUCCESS  outcome=OK
  outputs:
    savings_balance = {'amount': 18425.63, 'currency': 'USD', 'raw': '$18,425.63'}
    member_name = ALICE T. NGUYEN
    [ok  ] s1   click    e9  via role_name[0] p=0.97
    ...
  llm calls: 0   degraded locators: 0   8885 ms
```

`llm calls: 0` is the whole point — that run had no model in the decision loop.

### Every error class, by hand

Each `pcx fault` arms one fault for the next request only.

```bash
# declared business outcomes — results, not crashes
pcx replay read_member_savings_balance --input member_number=99999999   # MEMBER_NOT_FOUND
pcx replay read_member_savings_balance --input member_number=10000003   # PERMISSION_DENIED

# rejected at the contract boundary — the application is never touched
pcx replay read_member_savings_balance --input member_number=1000000    # INVALID_INPUT

# recoverable conditions the replay handles itself
pcx fault interstitial
pcx replay read_member_savings_balance --input member_number=10000002
pcx fault slow
pcx replay read_member_savings_balance --input member_number=10000001
pcx fault session_timeout
pcx replay read_member_savings_balance --input member_number=10000001

# a hard failure — screenshot and DOM snapshot written
pcx fault app_error
pcx replay read_member_savings_balance --input member_number=10000001

# the safety gate on an irreversible step
pcx replay post_transaction_guarded --input account=10000001-0000 --input amount=250.00
pcx replay post_transaction_guarded --input account=10000001-0000 --input amount=250.00 --approve
```

Or run all of them in sequence: `python scripts/demo.py --replay-only`.

Valid member numbers are `10000001`–`10000005`. `10000003` carries an advisory
hold; `99999999` does not exist.

### Watch the browser

```bash
# PowerShell:  $env:PCX_HEADLESS = "0"; pcx replay ...
# bash:        PCX_HEADLESS=0 pcx replay ...
```

Native Windows opens a real Chromium window. WSL does too on Windows 11 (WSLg);
on Windows 10 you would need an X server — not worth it, the annotated
screenshots under `evidence/` show the same thing.

## Multi-tenant — the same artifact, a second institution

With the second tenant running on 8798:

```bash
pcx replay read_member_savings_balance --tenant northgate_cu --input member_number=10000002
```

Same artifact, same digest, a seven-line overlay. It succeeds and prints a
`[drift]` line per screen — the flow works *and* the system knows this tenant's
screens differ from the recording.

## Escalation — a human takes over the live session

```bash
python scripts/demo_handoff.py
```

A replay hits an injected `CSV-500`, escalates, an operator claims it, clicks and
types on **the same live browser session**, releases control, and the engine
re-checks the checkpoint. Open <http://127.0.0.1:8765> while it runs.

To drive the console yourself instead:

```bash
pcx fault app_error
pcx replay read_member_savings_balance --input member_number=10000001 --escalate --console
```

Then open <http://127.0.0.1:8765>, click **Take control**, click your way to the
inquiry screen, type the member number, and press **Resume**.

## Crystallization — promotion and demotion

```bash
python scripts/demo_crystallize.py       # ~2 minutes, ~15 replays
```

Type 3 → 2 → 1 on accumulated evidence, a regression trips the circuit breaker
back to Type 2 + quarantine, and clean runs restore it. Inspect at any point with
`pcx status read_member_savings_balance`, which prints every gate with PASS/FAIL.

## Discovery — the LLM-driven half

**The only part that needs model access**, and it costs tokens (~$0.30-0.45 per run).

### Where the API key goes

**Easiest: a `.env` file in the repository root.** It is gitignored, it survives
closing the terminal, and it is loaded automatically by every entry point.

```dotenv
# .env  (copy .env.example)
ANTHROPIC_API_KEY=sk-ant-api03-...
PCX_OPERATOR_ID=TLR0042
PCX_OPERATOR_PASSWORD=openSesame!42
```

Every line must be `KEY=value`. A bare `ANTHROPIC_API_KEY` with no `=` is
**skipped** rather than set to an empty string — an empty key would read as
"configured" to every check downstream, which is a worse failure than none.

Or export it for the current terminal, which takes precedence over the file:

```powershell
$env:ANTHROPIC_API_KEY = "sk-ant-api03-..."     # PowerShell
```
```bash
export ANTHROPIC_API_KEY=sk-ant-api03-...        # bash / WSL / macOS
```

Get a key at <https://console.anthropic.com> → **API keys**. Confirm it is
actually visible to the process:

```bash
python scripts/doctor.py
```
```
[  ok  ] .env           — loaded: ANTHROPIC_API_KEY, PCX_OPERATOR_ID, PCX_OPERATOR_PASSWORD
[  ok  ] model backend  — sk-ant-api0…1234 from the .env
```

The doctor prints the key's first and last few characters and where it came
from, so a stale shell export shadowing your `.env` is visible rather than
mysterious. Nothing else ever prints it: it is read in one place
(`AnthropicMessagesClient.__init__`) and never written to an artifact, a log or
the evidence directory.

That is the whole setup. `pcx discover` picks the backend automatically: an API
key if one is set, otherwise the `claude` CLI if it is installed and logged in.
Force either with `--llm api` / `--llm cli`. Choose a different model with
`PCX_MODEL` (default `claude-sonnet-4-5-20250929`).

The key is read in one place — `AnthropicMessagesClient.__init__` in
`src/pcx/agent/llm.py` — and is never written to an artifact, a log or the
evidence directory. A rejected key fails on the first call with a message saying
so, rather than retrying three times.

### Watching a live run

`pcx discover` narrates every turn as it happens: what the model saw, what it
decided and why, what it did, and what that cost.

```bash
pcx discover \
  --id read_member_savings_balance \
  --goal "Look up member 10000002 in the CoreServ member inquiry screen and read back their current SHARE SAVINGS balance and their member name." \
  --entrypoint "http://127.0.0.1:8799/desk" \
  --setup sign_on_coreserv \
  --param member_number=10000002 \
  --output savings_balance --output member_name
```

*(PowerShell continues lines with a backtick `` ` ``, not `\` — or put it on one line.)*

```
discovery run  (claude-cli / sonnet)
  goal  Look up member 10000002 ... read back their current SHARE SAVINGS balance ...
  start http://127.0.0.1:8799/desk
  ------------------------------------------------------------------------
step 1  see    CoreServ 7.2 - Teller Workstation   4 controls, 17 elements
          screenshot: discovery-20260910T004907-677f/frames/012-step01-observe.png
        think  I'm at the teller workstation home screen; I need to navigate to Member Inquiry…  (6.5s, 62 out, $0.158)
        act    CLICK   e9  -> link 'MEMBER INQUIRY'

step 2  see    CoreServ 7.2 - Teller Workstation   6 controls, 19 elements
        think  I'm on the Member Inquiry screen with a member number input field (e8)…  (5.0s, 79 out, $0.048)
        act    TYPE    e8 '10000002'  -> textbox '(unlabelled)'

step 4  see    CoreServ 7.2 - Teller Workstation   6 controls, 44 elements
        think  The member detail screen shows NAME 'BERNARD O. HALLORAN' in e13 and SHARE SAVINGS…  (8.8s, 225 out, $0.069)
        act    EXTRACT e13 -> member_name  -> cell 'BERNARD O. HALLORAN'
        captured member_name = 'BERNARD O. HALLORAN'
  ------------------------------------------------------------------------
  succeeded  model reported the goal satisfied
  6 steps  43s  $0.43  690 output tokens
[compile] read_member_savings_balance@v2 -> capabilities/read_member_savings_balance/v2.yaml
```

Add `--quiet` to suppress it. Three other ways to watch the same run:

- **See the browser.** `PCX_HEADLESS=0` (PowerShell: `$env:PCX_HEADLESS = "0"`)
  opens a real Chromium window and you watch the agent click and type.
- **Open the screenshots as they are written.** Each `see` line prints the frame
  path; the PNGs have the numbered boxes drawn on them — the exact view the model
  was given. They appear in `evidence/discovery-*/frames/` during the run.
- **Tail the structured log.** `tail -f evidence/discovery-*/run.jsonl`
  (PowerShell: `Get-Content ... -Wait`) for the machine-readable version.

### How the model actually "learns" the capability

It does not learn in the weights sense, and it never writes the artifact. The
mechanism is worth being precise about, because the separation is the point:

1. **Perceive.** The surface adapter builds an element inventory from the
   accessibility tree — role, accessible name, value, rectangle, frame — and an
   annotated screenshot with the same reference numbers drawn on it.
2. **Decide.** The model receives the goal, the declared input parameters, the
   outputs it must capture, a one-line history of what it has already done, and
   the current screen. It replies with exactly one action as JSON:
   `{"thought": "...", "action": {"kind": "type", "target_ref": "e8", "text": "10000002"}}`.
   It addresses controls only by reference number; it cannot write a selector, a
   URL, or code.
3. **Act, and record.** The action is checked against the guardrails, executed as
   a coordinate click or keystrokes, and appended to a `RunTrace` along with the
   full element inventory that was on screen when the decision was made.
4. **Repeat** until the model emits `finish` — which it may only do after binding
   every declared output with an `extract` action. A value it merely asserts
   without extracting is rejected.
5. **Crystallize.** This is where the "learning" is captured, and it is
   **deterministic Python, not the model**. `pcx/artifact/compile.py` reads the
   trace and, for every action, proposes several independent hypotheses about how
   that control could be identified — by accessible name, by adjacent label, by
   row × column in a table, by position — then *tests each one against the screen
   the action was actually taken on* and keeps only those that resolve to exactly
   the element that was clicked. Typed values become `{{ inputs.x }}`. Outputs get
   types inferred from what was read. The result is `capabilities/<id>/vN.yaml`.

So the model's contribution is *a sequence of observed actions*. The artifact is
**derived** from what happened, not written by the model — which is why nothing
the model said can end up in the capability except through a field that was
explicitly modelled, and why the artifact is reviewable without reading the
transcript.

You can see both halves separately. The raw model exchanges are in
`evidence/discovery-*/llm/`; the compiled result is `capabilities/*/v1.yaml`. And
because the trace is the source, you can re-run the compiler without paying for
the model again:

```bash
pcx recompile evidence/discovery-<id>/trace.full.json --id read_member_savings_balance
```

### After the run

The new capability is version 2 if the compiled flow differs from version 1 —
a different step order produces a different digest, which is the versioning
working. `pcx list` shows both; `pcx replay` uses HEAD.

`python scripts/demo.py` runs both discovery runs plus the whole replay tour —
the full thread, about 3 minutes.

## Inspect the evidence

```bash
cat evidence/replay-transcript.md            # 12 deterministic runs
cat evidence/crystallization-lifecycle.log
cat evidence/handoff-transcript.log
```

The annotated screenshots — the numbered boxes the model was addressing — are in
`evidence/discovery-*/frames/`. Open them from the VS Code explorer.

## Full verification checklist

| # | Command | Expected |
|---|---|---|
| 1 | `python scripts/doctor.py` | `Everything is ready` |
| 2 | `python -m pytest tests/ -q` | `93 passed` |
| 3 | `pcx target` *(terminal 1)* | listening on 8799; page loads in the browser |
| 4 | `pcx list` | three capabilities |
| 5 | `pcx replay read_member_savings_balance --input member_number=10000001` | `SUCCESS`, two typed outputs, `llm calls: 0` |
| 6 | `pcx replay … --input member_number=99999999` | `BUSINESS_OUTCOME MEMBER_NOT_FOUND` |
| 7 | `pcx replay … --input member_number=1000000` | `FAILED INVALID_INPUT`, ~1 ms — app never touched |
| 8 | `pcx fault app_error` then replay | `FAILED APPLICATION_ERROR` + a `frame` and a `snapshot` path |
| 9 | `pcx replay post_transaction_guarded --input account=10000001-0000 --input amount=250.00` | `ESCALATED APPROVAL_REQUIRED` |
| 10 | second tenant + `pcx replay … --tenant northgate_cu --input member_number=10000002` | `SUCCESS` with `[drift]` lines |
| 11 | `python scripts/demo_handoff.py` | ends `success / OK_AFTER_HANDOFF` |
| 12 | `python scripts/demo_crystallize.py` | Type 3 → 2 → 1, demotion, recovery |
| 13 | `pcx discover …` *(needs a key)* | `succeeded`, a new `evidence/discovery-*` |

Rows 1–12 need no model access. Row 13 is the only one that spends tokens.

---

## Troubleshooting

**`pcx: command not found` / `'pcx' is not recognized`** — the venv is not active,
or `pip install -e .` was not run. `python -m pcx.cli …` works either way.

**`Activate.ps1 cannot be loaded because running scripts is disabled`** (Windows) —
`Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned`, or use
`.\.venv\Scripts\activate.bat` from cmd.exe.

**`Host system is missing dependencies to run browsers`** (WSL/Linux only) —
`sudo python -m playwright install-deps chromium`.

**`net::ERR_CONNECTION_REFUSED at http://127.0.0.1:8799/`** — the target app is
not running. Start `pcx target` in another terminal.

**`Address already in use`** — something is on 8799. Run the app elsewhere:
`pcx target --port 8899`, then point the client at it — PowerShell
`$env:PCX_BASE_URL = "http://127.0.0.1:8899"`, bash `export PCX_BASE_URL=...`.

**Replay fails at sign-on with `SESSION_EXPIRED`** — `PCX_OPERATOR_ID` /
`PCX_OPERATOR_PASSWORD` are unset in *this* terminal. In bash the password needs
single quotes (`!` is history expansion); in PowerShell double quotes are fine.

**`ModuleNotFoundError: pcx`** — run from the repository root, or `pip install -e .`.

**Garbled `←[1m` characters in the output** (older Windows console) — the demo
scripts detect Windows Terminal and disable colour elsewhere; if you still see
them, use Windows Terminal or PowerShell 7.

**Chromium very slow, or launches then dies, under WSL** — the repo is on
`/mnt/c/`. Move it into `~` (§B1). This is the one real WSL performance trap.

**Can't reach `127.0.0.1:8799` from the Windows browser while running in WSL** —
usually a VPN, or a WSL1 distro. `wsl -l -v` should say VERSION 2; convert with
`wsl --set-version Ubuntu 2`.

**`pcx discover` fails only on Windows with the `claude` CLI backend** — the CLI
installs as a `.cmd` shim, which is handled, but if it still misbehaves use the
API backend (`ANTHROPIC_API_KEY`) instead; it is the primary path anyway.

**A demo leaves capabilities in an odd state** — the recorded flow is never
damaged, only its evidence counters. `python scripts/demo_crystallize.py` resets
them at the start of every run, or:

```bash
pcx recompile evidence/discovery-<balance-run>/trace.full.json --id read_member_savings_balance
```
