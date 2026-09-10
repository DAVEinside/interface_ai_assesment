#!/usr/bin/env bash
# Regenerate the curated contents of ./evidence.
#
# The two discovery runs are preserved rather than re-run: they are genuine
# model-driven runs and re-running them costs tokens and produces a different
# (equally valid) trace. Everything else is replayed fresh so the committed
# evidence matches the code exactly.
set -uo pipefail
cd "$(dirname "$0")/.."
export PYTHONPATH="$PWD/src:$PWD"
export PCX_OPERATOR_ID="${PCX_OPERATOR_ID:-TLR0042}"
export PCX_OPERATOR_PASSWORD="${PCX_OPERATOR_PASSWORD:-openSesame!42}"
export PCX_FRAMES=always
pcx() { python3 -m pcx.cli "$@"; }

KEEP=$(mktemp -d)
for d in evidence/discovery-*; do [ -d "$d" ] && cp -r "$d" "$KEEP/"; done
rm -rf evidence/*
for d in "$KEEP"/*; do cp -r "$d" evidence/; done
rm -rf "$KEEP"

# The crystallization walk produces a dozen runs; keep only its transcript.
PCX_EVIDENCE=$(mktemp -d) python3 scripts/demo_crystallize.py 2>&1 \
  | sed -e 's/\x1b\[[0-9;]*m//g' > evidence/crystallization-lifecycle.log

run() { printf '\n### %s\n' "$1" >> evidence/replay-transcript.md; shift; pcx "$@" 2>&1 | sed -e 's/\x1b\[[0-9;]*m//g' >> evidence/replay-transcript.md; }
: > evidence/replay-transcript.md
{ echo "# Replay transcript"; echo; echo 'Every run below was produced by `pcx replay`, with no model in the decision loop (`llm calls: 0` on each). Faults are armed out of band through the mock application'"'"'s `/admin/fault` endpoint, which the agent'"'"'s own allowlist forbids it from reaching.'; } > evidence/replay-transcript.md

run "Happy path -- typed outputs returned to the caller" replay read_member_savings_balance --input member_number=10000001
run "Same capability, a different member -- the artifact is parameterized, not recorded data" replay read_member_savings_balance --input member_number=10000005
run "Declared business outcome: no such member" replay read_member_savings_balance --input member_number=99999999
run "Declared business outcome: the record exists and this operator may not see it" replay read_member_savings_balance --input member_number=10000003
run "Caller error: rejected at the contract boundary, application never touched" replay read_member_savings_balance --input member_number=1000000
pcx fault interstitial --count 1 > /dev/null
run "Recoverable: a maintenance interstitial, dismissed without the caller noticing" replay read_member_savings_balance --input member_number=10000002
pcx fault slow --count 1 > /dev/null
run "Recoverable: a 4.5s host stall" replay read_member_savings_balance --input member_number=10000001
pcx fault session_timeout --count 1 > /dev/null
run "Recoverable: session expiry mid-flow, re-auth by capability composition, read-only flow restarted" replay read_member_savings_balance --input member_number=10000001
pcx fault app_error --count 1 > /dev/null
run "Hard failure: CSV-500, with a screenshot and a DOM snapshot" replay read_member_savings_balance --input member_number=10000001
run "Guardrail: an irreversible step with no operator available" replay post_transaction_guarded --input account=10000001-0000 --input amount=250.00
run "Guardrail: the same step after explicit human approval" replay post_transaction_guarded --input account=10000001-0000 --input amount=250.00 --approve
run "Multi-tenant: the same artifact against a second institution (CUSTOMER vocabulary, port 8798)" replay read_member_savings_balance --tenant northgate_cu --input member_number=10000002

python3 scripts/demo_handoff.py 2>&1 | sed -e 's/\x1b\[[0-9;]*m//g' > evidence/handoff-transcript.log
echo "evidence regenerated"
