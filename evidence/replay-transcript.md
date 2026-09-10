# Replay transcript

Every run below was produced by `pcx replay`, with no model in the decision loop (`llm calls: 0` on each). Faults are armed out of band through the mock application's `/admin/fault` endpoint, which the agent's own allowlist forbids it from reaching.

### Happy path -- typed outputs returned to the caller

[replay] SUCCESS  outcome=OK
  outputs:
    savings_balance = {'amount': 18425.63, 'currency': 'USD', 'raw': '$18,425.63'}
    member_name = ALICE T. NGUYEN
    [ok  ] s1   click    e9 via role_name[0] p=0.97
    [ok  ] s2   type     e8 via label[0] p=0.90
    [ok  ] s3   click    e9 via role_name[0] p=0.97
    [ok  ] s4   extract  e35 via label[0] p=0.90
    [ok  ] s5   extract  e13 via label[0] p=0.90
    [rcvr] reauth: sign_on_coreserv (re-auth success)
  llm calls: 0   degraded locators: 0   8885 ms
  evidence : /home/claude/pcx/evidence/replay-20260909T042006-9120

### Same capability, a different member -- the artifact is parameterized, not recorded data

[replay] SUCCESS  outcome=OK
  outputs:
    savings_balance = {'amount': 3312.87, 'currency': 'USD', 'raw': '$3,312.87'}
    member_name = EUNICE P. FARRAR
    [ok  ] s1   click    e9 via role_name[0] p=0.97
    [ok  ] s2   type     e8 via label[0] p=0.90
    [ok  ] s3   click    e9 via role_name[0] p=0.97
    [ok  ] s4   extract  e35 via label[0] p=0.90
    [ok  ] s5   extract  e13 via label[0] p=0.90
    [rcvr] reauth: sign_on_coreserv (re-auth success)
  llm calls: 0   degraded locators: 0   8904 ms
  evidence : /home/claude/pcx/evidence/replay-20260909T042016-0889

### Declared business outcome: no such member

[replay] BUSINESS_OUTCOME  outcome=MEMBER_NOT_FOUND
  The member number is well-formed but no such record exists on the member file. A legitimate answer to a lookup, not an error.
  remediation: Confirm the member number with the caller; try a name search instead.
    [ok  ] s1   click    e9 via role_name[0] p=0.97
    [ok  ] s2   type     e8 via label[0] p=0.90
    [ok  ] s3   click    e9 via role_name[0] p=0.97
    [rcvr] reauth: sign_on_coreserv (re-auth success)
  llm calls: 0   degraded locators: 0   7238 ms
  evidence : /home/claude/pcx/evidence/replay-20260909T042026-bf2a

### Declared business outcome: the record exists and this operator may not see it

[replay] BUSINESS_OUTCOME  outcome=PERMISSION_DENIED
  The record carries an advisory hold, or the signed-on operator's role is not authorized for this function. Distinct from NOT_FOUND: the record exists and the caller may not see it.
  remediation: Route to a supervisor or to Compliance; do not retry with the same operator.
    [ok  ] s1   click    e9 via role_name[0] p=0.97
    [ok  ] s2   type     e8 via label[0] p=0.90
    [ok  ] s3   click    e9 via role_name[0] p=0.97
    [rcvr] reauth: sign_on_coreserv (re-auth success)
  llm calls: 0   degraded locators: 0   7039 ms
  evidence : /home/claude/pcx/evidence/replay-20260909T042035-22f3

### Caller error: rejected at the contract boundary, application never touched

[replay] FAILED  outcome=INVALID_INPUT
  input 'member_number' does not match /\d{8}/
  remediation: Correct the inputs; the flow was not started.
  failure : INVALID_INPUT at None ()
    expected: inputs satisfying the capability contract
    observed: input 'member_number' does not match /\d{8}/
    hint    : Rejected at the contract boundary -- the application was never touched.
  llm calls: 0   degraded locators: 0   1 ms
  evidence : /home/claude/pcx/evidence/replay-20260909T042043-c64e

### Recoverable: a maintenance interstitial, dismissed without the caller noticing

[replay] SUCCESS  outcome=OK
  outputs:
    savings_balance = {'amount': 904.12, 'currency': 'USD', 'raw': '$904.12'}
    member_name = BERNARD O. HALLORAN
    [ok  ] s1   click    e9 via role_name[0] p=0.97
    [ok  ] s2   type     e8 via label[0] p=0.90
    [ok  ] s3   click    e9 via role_name[0] p=0.97
    [ok  ] s4   extract  e35 via label[0] p=0.90
    [ok  ] s5   extract  e13 via label[0] p=0.90
    [rcvr] reauth: sign_on_coreserv (re-auth success)
    [rcvr] interstitial: system_notice (dismissed)
  llm calls: 0   degraded locators: 0   9785 ms
  evidence : /home/claude/pcx/evidence/replay-20260909T042044-8622

### Recoverable: a 4.5s host stall

[replay] SUCCESS  outcome=OK
  outputs:
    savings_balance = {'amount': 18425.63, 'currency': 'USD', 'raw': '$18,425.63'}
    member_name = ALICE T. NGUYEN
    [ok  ] s1   click    e9 via role_name[0] p=0.97
    [ok  ] s2   type     e8 via label[0] p=0.90
    [ok  ] s3   click    e9 via role_name[0] p=0.97
    [ok  ] s4   extract  e35 via label[0] p=0.90
    [ok  ] s5   extract  e13 via label[0] p=0.90
    [rcvr] reauth: sign_on_coreserv (re-auth success)
  llm calls: 0   degraded locators: 0   13424 ms
  evidence : /home/claude/pcx/evidence/replay-20260909T042056-5056

### Recoverable: session expiry mid-flow, re-auth by capability composition, read-only flow restarted

[replay] SUCCESS  outcome=OK
  outputs:
    savings_balance = {'amount': 18425.63, 'currency': 'USD', 'raw': '$18,425.63'}
    member_name = ALICE T. NGUYEN
    [ok  ] s1   click    e9 via role_name[0] p=0.97
    [ok  ] s2   type     e8 via label[0] p=0.90
    [ok  ] s3   click    e9 via role_name[0] p=0.97
    [ok  ] s4   extract  e35 via label[0] p=0.90
    [ok  ] s5   extract  e13 via label[0] p=0.90
    [rcvr] reauth: sign_on_coreserv (re-auth success)
    [rcvr] reauth: sign_on_coreserv (re-auth success)
    [rcvr] reauth: reauth_restart (session expired mid-flow; read-only capability restarted from the first step)
  llm calls: 0   degraded locators: 0   14009 ms
  evidence : /home/claude/pcx/evidence/replay-20260909T042111-3efd

### Hard failure: CSV-500, with a screenshot and a DOM snapshot

[replay] FAILED  outcome=APPLICATION_ERROR
    [ok  ] s1   click    e9 via role_name[0] p=0.97
    [ok  ] s2   type     e8 via label[0] p=0.90
    [ok  ] s3   click    e9 via role_name[0] p=0.97
    [rcvr] reauth: sign_on_coreserv (re-auth success)
  failure : APPLICATION_ERROR at s3 (The member number is filled in and I need to click INQUIRE to look up member {{ inputs.member_number }}.)
    expected: {"route_matches":"^/desk$"}
    observed: any_of[text_matches /CSV-500/ -> True; text_present 'CORESERV SYSTEM ERROR' -> True]
    hint    : Application error page reached before the step could run.
    frame   : replay-20260909T042126-d2cd/frames/019-s3-apperror.png
  llm calls: 0   degraded locators: 0   7172 ms
  evidence : /home/claude/pcx/evidence/replay-20260909T042126-d2cd

### Guardrail: an irreversible step with no operator available

[replay] ESCALATED  outcome=APPROVAL_REQUIRED
    [ok  ] s1   click    e14 via role_name[0] p=0.97
    [ok  ] s2   type     e7 via label[0] p=0.90
    [ok  ] s3   type     e14 via label[0] p=0.90
    [rcvr] reauth: sign_on_coreserv (re-auth success)
  failure : APPROVAL_REQUIRED at s4 (Commit the posting to the core ledger)
    expected: {"text_present":"POSTING COMMITTED"}
    observed: step 's4' is classified irreversible ('POST TRANSACTION button') and requires human approval
    hint    : This step is irreversible and no operator was available to approve it. Re-invoke with an operator console attached, or pass an explicit pre-approval. The run stopped before the step, not during it.
  llm calls: 0   degraded locators: 0   7028 ms
  evidence : /home/claude/pcx/evidence/replay-20260909T042134-f42a

### Guardrail: the same step after explicit human approval

[replay] SUCCESS  outcome=OK
    [ok  ] s1   click    e14 via role_name[0] p=0.97
    [ok  ] s2   type     e7 via label[0] p=0.90
    [ok  ] s3   type     e14 via label[0] p=0.90
    [ok  ] s4   click    e19 via role_name[0] p=0.97
    [rcvr] reauth: sign_on_coreserv (re-auth success)
  llm calls: 0   degraded locators: 0   8036 ms
  evidence : /home/claude/pcx/evidence/replay-20260909T042143-28e2

### Multi-tenant: the same artifact against a second institution (CUSTOMER vocabulary, port 8798)

[replay] SUCCESS  outcome=OK
  outputs:
    savings_balance = {'amount': 904.12, 'currency': 'USD', 'raw': '$904.12'}
    member_name = BERNARD O. HALLORAN
    [ok  ] s1   click    e9 via role_name[0] p=0.97
    [ok  ] s2   type     e8 via label[0] p=0.90
    [ok  ] s3   click    e9 via role_name[0] p=0.97
    [ok  ] s4   extract  e35 via label[0] p=0.90
    [ok  ] s5   extract  e13 via label[0] p=0.90
    [rcvr] reauth: sign_on_coreserv (re-auth success)
    [drift] s1: screen sig_143206eddb3a9294 != recorded sig_2abe72c1f62fc5f1
    [drift] s2: screen sig_143206eddb3a9294 != recorded sig_2abe72c1f62fc5f1
    [drift] s3: screen sig_bf581ff455af2633 != recorded sig_c68985f8331cb3b6
    [drift] s4: screen sig_bf581ff455af2633 != recorded sig_c68985f8331cb3b6
    [drift] s5: screen sig_bf581ff455af2633 != recorded sig_c68985f8331cb3b6
    [drift] checkpoint: screen sig_bf581ff455af2633 != recorded sig_c68985f8331cb3b6
  llm calls: 0   degraded locators: 0   8981 ms
  evidence : /home/claude/pcx/evidence/replay-20260909T042152-1afb
