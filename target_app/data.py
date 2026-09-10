"""Seed data for the mock CoreServ 7.2 teller workstation.

ALL DATA IS SYNTHETIC. Member numbers, names, SSNs and balances are invented for
this demo. The SSN field exists specifically so the redaction pipeline in
``pcx.policy.redact`` has something real to scrub out of traces and artifacts.
"""

from __future__ import annotations

from decimal import Decimal

OPERATORS = {
    # operator_id -> (password, display name, role, permissions)
    "TLR0042": ("openSesame!42", "R. OKONKWO", "TELLER", {"inquiry", "open_subaccount"}),
    "TLR0007": ("hunter2hunter2", "M. DUBOIS", "TELLER_TRAINEE", {"inquiry"}),
}

# status: OK | RESTRICTED | CLOSED
MEMBERS = {
    "10000001": {
        "name": "ALICE T. NGUYEN",
        "ssn": "537-88-4821",
        "dob": "1979-04-11",
        "branch": "003 - RIVERSIDE",
        "status": "OK",
        "member_since": "2004-08-19",
        "accounts": [
            {"suffix": "0000", "type": "SHARE SAVINGS", "balance": Decimal("18425.63"), "status": "ACTIVE"},
            {"suffix": "0010", "type": "SHARE DRAFT CHECKING", "balance": Decimal("2140.09"), "status": "ACTIVE"},
            {"suffix": "0700", "type": "AUTO LOAN", "balance": Decimal("-11208.44"), "status": "ACTIVE"},
        ],
    },
    "10000002": {
        "name": "BERNARD O. HALLORAN",
        "ssn": "412-05-9930",
        "dob": "1961-11-30",
        "branch": "001 - MAIN OFFICE",
        "status": "OK",
        "member_since": "1996-02-05",
        "accounts": [
            {"suffix": "0000", "type": "SHARE SAVINGS", "balance": Decimal("904.12"), "status": "ACTIVE"},
            {"suffix": "0020", "type": "MONEY MARKET", "balance": Decimal("52310.00"), "status": "ACTIVE"},
        ],
    },
    "10000003": {
        "name": "CARLA J. MBEKI",
        "ssn": "601-22-7714",
        "dob": "1988-07-02",
        "branch": "007 - NORTHGATE",
        "status": "RESTRICTED",  # advisory hold -> permission denial on inquiry
        "member_since": "2011-05-23",
        "accounts": [
            {"suffix": "0000", "type": "SHARE SAVINGS", "balance": Decimal("77.10"), "status": "HOLD"},
        ],
    },
    "10000004": {
        "name": "DIMITRI S. VOLKOV",
        "ssn": "289-41-3355",
        "dob": "1955-01-17",
        "branch": "003 - RIVERSIDE",
        "status": "OK",
        "member_since": "1988-09-01",
        "accounts": [
            {"suffix": "0000", "type": "SHARE SAVINGS", "balance": Decimal("0.00"), "status": "DORMANT"},
        ],
    },
    "10000005": {
        "name": "EUNICE P. FARRAR",
        "ssn": "774-19-0028",
        "dob": "1993-03-28",
        "branch": "012 - SOUTH PLAZA",
        "status": "OK",
        "member_since": "2019-10-14",
        "accounts": [
            {"suffix": "0000", "type": "SHARE SAVINGS", "balance": Decimal("3312.87"), "status": "ACTIVE"},
        ],
    },
}

SUBACCOUNT_TYPES = [
    ("0030", "VACATION CLUB"),
    ("0040", "HOLIDAY CLUB"),
    ("0050", "CERTIFICATE 12MO"),
]

# Faults the operator can arm through /admin/fault. Each fires once unless sticky.
FAULTS = {
    "none": "No fault armed",
    "slow": "Member inquiry stalls behind an INTERIM PLEASE WAIT page",
    "interstitial": "A SYSTEM NOTICE interstitial is injected before the next screen",
    "session_timeout": "The session is invalidated; next request lands on the login screen",
    "app_error": "CoreServ raises an unhandled system error (CSV-500)",
}
