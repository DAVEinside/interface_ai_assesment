"""CoreServ 7.2 -- Teller Workstation (mock).

A deliberately *legacy* back-office web application, standing in for the kind of
internal system US credit unions actually run. It is built to be hostile to the
usual browser-automation shortcuts:

  * a real ``<frameset>`` shell (navigation frame + work frame),
  * nested ``<table>`` layout, ``<font>`` tags, no CSS classes on controls,
  * ASP.NET-style generated ids (``ctl00_wf_txt3``) that change between screens,
  * form fields named ``f1``/``f2`` with no ``<label for=...>`` association, so
    the accessibility tree exposes them with an *empty* accessible name,
  * server-rendered full-page navigation, no JSON API.

It also emits the runtime conditions the replay engine has to survive: validation
errors, record-not-found, permission denial, an interstitial notice, session
expiry, transient slowness and a hard application error. Faults are armed out of
band via ``/admin/fault`` so a replay can be pushed into a failure mode on demand.

Run:  python -m target_app.app  (listens on 127.0.0.1:8799)
"""

from __future__ import annotations

import os
import random
import time
from decimal import Decimal

from flask import (
    Flask,
    make_response,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from .data import FAULTS, MEMBERS, OPERATORS, SUBACCOUNT_TYPES

app = Flask(__name__, template_folder="templates")
app.secret_key = os.environ.get("TARGET_APP_SECRET", "coreserv-demo-not-a-real-secret")

#: Armed fault. Set through /admin/fault. Consumed by _fire_fault().
_FAULT = {"name": "none", "remaining": 0}

#: Monotonic confirmation-number source, so evidence is reproducible-ish.
_CONFIRMATION_SEQ = {"n": 480917}


def _fire_fault(name: str) -> bool:
    """Return True (and consume one charge) if fault ``name`` is armed."""
    if _FAULT["name"] == name and _FAULT["remaining"] > 0:
        _FAULT["remaining"] -= 1
        if _FAULT["remaining"] == 0:
            _FAULT["name"] = "none"
        return True
    return False


def _require_session():
    if not session.get("operator"):
        return redirect(url_for("login", expired=1))
    return None


def _money(value: Decimal) -> str:
    sign = "-" if value < 0 else ""
    return f"{sign}${abs(value):,.2f}"


# --------------------------------------------------------------------------- #
# Fault administration (out of band -- never part of the agent's allowlist)
# --------------------------------------------------------------------------- #
@app.route("/admin/fault", methods=["GET", "POST"])
def admin_fault():
    name = (request.values.get("name") or "none").lower()
    count = int(request.values.get("count") or 1)
    if name not in FAULTS:
        return {"error": f"unknown fault {name}", "known": sorted(FAULTS)}, 400
    _FAULT["name"] = name
    _FAULT["remaining"] = count if name != "none" else 0
    return {"armed": _FAULT["name"], "remaining": _FAULT["remaining"]}


@app.route("/admin/reset", methods=["GET", "POST"])
def admin_reset():
    _FAULT.update({"name": "none", "remaining": 0})
    session.clear()
    return {"ok": True}


# --------------------------------------------------------------------------- #
# Authentication
# --------------------------------------------------------------------------- #
@app.route("/", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        op = (request.form.get("f1") or "").strip().upper()
        pw = request.form.get("f2") or ""
        record = OPERATORS.get(op)
        if not record or record[0] != pw:
            return render_template(
                "login.html",
                error="CSV-013  INVALID OPERATOR ID OR PASSWORD",
                expired=False,
            )
        session["operator"] = op
        session["operator_name"] = record[1]
        session["role"] = record[2]
        session["perms"] = sorted(record[3])
        return redirect(url_for("desk"))
    return render_template(
        "login.html",
        error=None,
        expired=bool(request.args.get("expired")),
    )


@app.route("/signoff")
def signoff():
    session.clear()
    return redirect(url_for("login"))


# --------------------------------------------------------------------------- #
# Frameset shell
# --------------------------------------------------------------------------- #
@app.route("/desk")
def desk():
    guard = _require_session()
    if guard:
        return guard
    return render_template("desk.html")


@app.route("/nav")
def nav():
    guard = _require_session()
    if guard:
        return guard
    return render_template("nav.html", operator=session.get("operator_name", ""))


@app.route("/welcome")
def welcome():
    guard = _require_session()
    if guard:
        return guard
    return render_template(
        "welcome.html",
        operator=session.get("operator_name", ""),
        role=session.get("role", ""),
    )


# --------------------------------------------------------------------------- #
# Member inquiry
# --------------------------------------------------------------------------- #
@app.route("/inquiry", methods=["GET", "POST"])
def inquiry():
    guard = _require_session()
    if guard:
        return guard

    if _fire_fault("session_timeout"):
        session.clear()
        return redirect(url_for("login", expired=1))

    if _fire_fault("interstitial"):
        return render_template(
            "notice.html",
            headline="SYSTEM NOTICE",
            body=(
                "SCHEDULED MAINTENANCE WINDOW 02:00-04:00 CT. "
                "CORE POSTING MAY BE DELAYED. ACKNOWLEDGE TO CONTINUE."
            ),
            back=request.full_path,
        )

    if request.method == "GET":
        return render_template("inquiry_form.html", error=None)

    if _fire_fault("app_error"):
        return render_template("error500.html"), 500

    if _fire_fault("slow"):
        # The host link is congested. Legacy apps do not stream a spinner; the
        # socket simply hangs until the mainframe answers.
        time.sleep(float(os.environ.get("TARGET_APP_SLOW_SECONDS", "4.5")))

    raw = (request.form.get("f1") or "").strip()
    if not raw:
        return render_template(
            "inquiry_form.html", error="MBR-001  MEMBER NUMBER IS REQUIRED"
        )
    if not raw.isdigit() or len(raw) != 8:
        return render_template(
            "inquiry_form.html", error="MBR-002  MEMBER NUMBER MUST BE 8 DIGITS"
        )

    member = MEMBERS.get(raw)
    if member is None:
        return render_template(
            "inquiry_form.html", error="MBR-404  MEMBER NOT FOUND ON FILE"
        )

    if member["status"] == "RESTRICTED":
        return render_template(
            "denied.html",
            code="SEC-403",
            message="PERMISSION DENIED - ADVISORY HOLD ON RECORD",
            detail="CONTACT COMPLIANCE (X4180) FOR RELEASE AUTHORIZATION.",
        )

    time.sleep(random.uniform(0.05, 0.15))  # a little jitter, like a real host call
    rows = [
        {
            "suffix": a["suffix"],
            "type": a["type"],
            "balance": _money(a["balance"]),
            "status": a["status"],
        }
        for a in member["accounts"]
    ]
    return render_template(
        "member_detail.html",
        member_number=raw,
        member=member,
        rows=rows,
        as_of=time.strftime("%Y-%m-%d"),
    )


# --------------------------------------------------------------------------- #
# Sub-account opening (a reversible-write flow with a confirmation gate)
# --------------------------------------------------------------------------- #
@app.route("/subacct", methods=["GET", "POST"])
def subacct():
    guard = _require_session()
    if guard:
        return guard
    if "open_subaccount" not in session.get("perms", []):
        return render_template(
            "denied.html",
            code="SEC-401",
            message="PERMISSION DENIED - OPERATOR NOT AUTHORIZED FOR ACCOUNT OPENING",
            detail="ROLE TELLER_TRAINEE MAY PERFORM INQUIRY ONLY.",
        )

    if request.method == "GET":
        return render_template(
            "subacct_form.html",
            member_number=request.args.get("mbr", ""),
            types=SUBACCOUNT_TYPES,
            error=None,
        )

    mbr = (request.form.get("f1") or "").strip()
    suffix = (request.form.get("f2") or "").strip()
    deposit = (request.form.get("f3") or "").strip()
    stage = request.form.get("stage") or "review"

    member = MEMBERS.get(mbr)
    if member is None:
        return render_template(
            "subacct_form.html",
            member_number=mbr,
            types=SUBACCOUNT_TYPES,
            error="MBR-404  MEMBER NOT FOUND ON FILE",
        )
    try:
        amount = Decimal(deposit or "0")
    except Exception:
        return render_template(
            "subacct_form.html",
            member_number=mbr,
            types=SUBACCOUNT_TYPES,
            error="AMT-002  INITIAL DEPOSIT IS NOT A VALID AMOUNT",
        )
    if amount < Decimal("25.00"):
        return render_template(
            "subacct_form.html",
            member_number=mbr,
            types=SUBACCOUNT_TYPES,
            error="AMT-114  INITIAL DEPOSIT BELOW PRODUCT MINIMUM OF $25.00",
        )

    label = dict(SUBACCOUNT_TYPES).get(suffix, "UNKNOWN")
    if stage == "review":
        return render_template(
            "subacct_review.html",
            member_number=mbr,
            member=member,
            suffix=suffix,
            label=label,
            amount=_money(amount),
            raw_amount=str(amount),
        )

    _CONFIRMATION_SEQ["n"] += 1
    return render_template(
        "subacct_done.html",
        member_number=mbr,
        member=member,
        suffix=suffix,
        label=label,
        amount=_money(amount),
        confirmation=f"CF{_CONFIRMATION_SEQ['n']}",
    )


# --------------------------------------------------------------------------- #
# Transaction posting -- present purely as an irreversible action the policy
# engine must refuse to let the agent touch autonomously.
# --------------------------------------------------------------------------- #
@app.route("/posting", methods=["GET", "POST"])
def posting():
    guard = _require_session()
    if guard:
        return guard
    if request.method == "POST":
        return render_template(
            "notice.html",
            headline="POSTING COMMITTED",
            body="TRANSACTION POSTED TO CORE. THIS ACTION CANNOT BE REVERSED FROM THIS SCREEN.",
            back=url_for("welcome"),
        )
    return render_template("posting_form.html")


#: A second "tenant" of the same vendor product: identical code paths, different
#: branding and vocabulary. This is what most multi-tenant drift actually looks
#: like -- the same CoreServ build, configured by the institution. Set
#: TARGET_APP_VOCAB=customer to run this instance as Northgate CU.
_VOCAB = {
    "customer": [
        ("MERIDIAN CREDIT UNION", "NORTHGATE FEDERAL CU"),
        ("MEMBER", "CUSTOMER"),
        ("Member", "Customer"),
    ]
}


@app.after_request
def _no_cache(resp):
    resp.headers["Cache-Control"] = "no-store"
    swaps = _VOCAB.get(os.environ.get("TARGET_APP_VOCAB", ""))
    if swaps and resp.content_type.startswith("text/html"):
        body = resp.get_data(as_text=True)
        for old, new in swaps:
            body = body.replace(old, new)
        resp.set_data(body)
    return resp


@app.errorhandler(500)
def _five_hundred(_e):
    return make_response(render_template("error500.html"), 500)


def main(port: int | None = None, vocab: str | None = None, secret: str | None = None) -> None:
    """Run the mock application.

    Arguments win over environment variables so the CLI can start a second
    tenant without the caller needing shell-specific `export` / `set` syntax.
    """
    if vocab is not None:
        os.environ["TARGET_APP_VOCAB"] = vocab
    if secret is not None:
        app.secret_key = secret
    port = port or int(os.environ.get("TARGET_APP_PORT", "8799"))
    app.run(host="127.0.0.1", port=port, threaded=True, debug=False)


if __name__ == "__main__":
    main()
