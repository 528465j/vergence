#!/usr/bin/env python3
"""Build the synthetic ledgers and the defect manifest that tests them.

This is a data generator. It is not part of the pipeline and imports nothing
from src/, deliberately: the manifest it writes is the expected-results oracle
for the control tests, and an oracle derived from the code under test proves
only that the code agrees with itself.

It builds a clean General Ledger and a Trial Balance derived from it for one
client, verifies both, injects five defects, verifies the result, records every
injected defect in a machine-readable manifest, and writes a second clean
ledger for a client whose file format differs. Files are written only after
every assertion has passed, so a failed run leaves the previous data in place.

Run:

    python tools/make_synthetic_data.py

Exits 0 having written four files under data/, or prints the failing assertion
with its expected and actual values, writes nothing, and exits 1.
"""

from __future__ import annotations

import csv
import json
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = REPO_ROOT / "config"
DATA_DIR = REPO_ROOT / "data"

CENTS = Decimal("0.01")
ZERO = Decimal("0.00")


def money(value) -> Decimal:
    """Every monetary value in this file passes through here.

    Parsed from a string and quantised to two places. Never float: binary
    floating point would introduce differences that look like reconciliation
    breaks but are not, which in a generator of known defects would be fatal.
    """
    return Decimal(str(value)).quantize(CENTS, rounding=ROUND_HALF_UP)


def dec_str(value: Decimal) -> str:
    """Render a quantised Decimal for a CSV cell or a manifest field."""
    return f"{value:.2f}"


def as_date(value) -> date:
    """YAML gives back a date for an unquoted date; a string otherwise."""
    return value if isinstance(value, date) else date.fromisoformat(str(value))


# ---------------------------------------------------------------------------
# Verification scaffolding
# ---------------------------------------------------------------------------


class VerificationFailure(Exception):
    """A property the generated data was required to hold does not hold."""

    def __init__(self, ident: str, description: str, expected, actual) -> None:
        super().__init__(f"{ident}: {description}")
        self.ident = ident
        self.description = description
        self.expected = expected
        self.actual = actual


def check(ident: str, description: str, ok: bool, expected, actual) -> None:
    """Assert one property. The failure path reports; it never repairs.

    A generator that adjusted its data until an assertion passed would be
    manufacturing the answer it is meant to be checking.
    """
    if not ok:
        raise VerificationFailure(ident, description, expected, actual)
    print(f"PASS  {ident:<4} {description}")


def heading(text: str) -> None:
    print(f"\n{text}\n{'-' * len(text)}")


def note(text: str) -> None:
    for line in text.splitlines():
        print(f"      {line}")


# ---------------------------------------------------------------------------
# Client A: chart of accounts and journals
# ---------------------------------------------------------------------------

ENTITY_A = "AU01"
CURRENCY_A = "AUD"
SOURCE_SYSTEM_A = "ERP-X"

# code, name, type, opening balance on the debit-positive convention.
# The opening balances sum to exactly 0.00; G5 re-derives that rather than
# trusting the table.
CHART_OF_ACCOUNTS_A = (
    ("1000", "Cash at Bank", "Asset", "250000.00"),
    ("1100", "Trade Receivables", "Asset", "180000.00"),
    ("1500", "Inventory", "Asset", "95000.00"),
    ("2000", "Trade Payables", "Liability", "-120000.00"),
    ("3000", "Retained Earnings", "Equity", "-405000.00"),
    ("4100", "Sales Revenue", "Income", "0.00"),
    ("5000", "Cost of Sales", "Expense", "0.00"),
    ("6000", "Operating Expenses", "Expense", "0.00"),
)

# (journal_id, posting date, [(account_code, description, debit, credit), ...])
#
# Every journal is an ordinary two-sided bookkeeping entry, so debits equal
# credits within it by construction rather than by adjustment. Four properties
# are load-bearing and are re-derived from the built data by V1 to V7 and by
# the extra guards, not taken on trust here:
#
#   * account 1500 Inventory is never named, so it carries an opening balance
#     and no movement. That is defect D5, which needs no injection;
#   * every account other than 1500 is posted to at least once. Account 3000
#     carries a dividend for exactly this reason: an equity account with no
#     movement would raise a second completeness exception and the expected
#     count of 8 would no longer hold;
#   * account 4100 is credited 347500.00 across five sales journals, clearing
#     the 300000.00 revenue floor;
#   * every journal has its own posting date, so no two lines can collide on
#     the suspected-duplicate test of account, date and amount across journals.
#
# J-1015 line 1 is the 1200.00 expense that D2 duplicates. J-1042 is the
# year-end journal that D1 unbalances. J-1003 line 1 is the posting D4 moves
# out of the financial year.
JOURNALS_A = (
    ("J-1001", "2025-07-08", (
        ("1100", "Sales invoiced on credit - batch 1", "52000.00", "0.00"),
        ("1100", "Sales invoiced on credit - batch 2", "36500.00", "0.00"),
        ("4100", "Sales revenue - July", "0.00", "88500.00"),
    )),
    ("J-1002", "2025-07-22", (
        ("5000", "Goods received - supplier batch 1", "28400.00", "0.00"),
        ("5000", "Goods received - supplier batch 2", "12600.00", "0.00"),
        ("2000", "Supplier invoices payable - July", "0.00", "41000.00"),
    )),
    ("J-1003", "2025-08-05", (
        ("1000", "Customer receipt - deposit 1", "40000.00", "0.00"),
        ("1000", "Customer receipt - deposit 2", "25000.00", "0.00"),
        ("1100", "Receivables settled - August", "0.00", "65000.00"),
    )),
    ("J-1004", "2025-09-14", (
        ("1100", "Sales invoiced on credit", "44000.00", "0.00"),
        ("4100", "Sales revenue - September", "0.00", "44000.00"),
    )),
    ("J-1005", "2025-09-30", (
        ("6000", "Premises costs - quarter 1", "12500.00", "0.00"),
        ("6000", "Utilities - quarter 1", "3750.00", "0.00"),
        ("6000", "Insurance - quarter 1", "1980.00", "0.00"),
        ("1000", "Operating expenses settled", "0.00", "18230.00"),
    )),
    ("J-1006", "2025-10-16", (
        ("1100", "Sales invoiced on credit - batch 1", "48750.00", "0.00"),
        ("1100", "Sales invoiced on credit - batch 2", "23750.00", "0.00"),
        ("4100", "Sales revenue - October", "0.00", "72500.00"),
    )),
    ("J-1007", "2025-10-31", (
        ("2000", "Supplier payment - run 1", "22000.00", "0.00"),
        ("2000", "Supplier payment - run 2", "16000.00", "0.00"),
        ("1000", "Bank payment to suppliers", "0.00", "38000.00"),
    )),
    ("J-1008", "2025-11-20", (
        ("5000", "Goods received - supplier batch 1", "27400.00", "0.00"),
        ("5000", "Goods received - supplier batch 2", "9600.00", "0.00"),
        ("2000", "Supplier invoices payable - November", "0.00", "37000.00"),
    )),
    ("J-1009", "2025-12-12", (
        ("1000", "Customer receipt - deposit 1", "55000.00", "0.00"),
        ("1000", "Customer receipt - deposit 2", "36000.00", "0.00"),
        ("1100", "Receivables settled - December", "0.00", "91000.00"),
    )),
    ("J-1010", "2025-12-31", (
        ("3000", "Dividend declared and paid", "30000.00", "0.00"),
        ("1000", "Dividend payment from bank", "0.00", "30000.00"),
    )),
    ("J-1011", "2026-01-23", (
        ("1100", "Sales invoiced on credit - batch 1", "41250.00", "0.00"),
        ("1100", "Sales invoiced on credit - batch 2", "24750.00", "0.00"),
        ("4100", "Sales revenue - January", "0.00", "66000.00"),
    )),
    ("J-1012", "2026-02-13", (
        ("6000", "Payroll - February", "21300.00", "0.00"),
        ("6000", "Superannuation - February", "4900.00", "0.00"),
        ("6000", "Office administration - February", "2150.00", "0.00"),
        ("1000", "Payroll and administration settled", "0.00", "28350.00"),
    )),
    ("J-1013", "2026-03-19", (
        ("5000", "Goods received - March", "33200.00", "0.00"),
        ("6000", "Inward freight - March", "2800.00", "0.00"),
        ("2000", "Supplier invoices payable - March", "0.00", "36000.00"),
    )),
    ("J-1014", "2026-04-24", (
        ("1100", "Sales invoiced on credit - batch 1", "57000.00", "0.00"),
        ("1100", "Sales invoiced on credit - batch 2", "19500.00", "0.00"),
        ("4100", "Sales revenue - April", "0.00", "76500.00"),
    )),
    ("J-1015", "2026-05-15", (
        ("6000", "Bank charges - May", "1200.00", "0.00"),
        ("6000", "Premises costs - May", "7450.00", "0.00"),
        ("6000", "Professional fees - May", "3300.00", "0.00"),
        ("1000", "Operating expenses settled", "0.00", "11950.00"),
    )),
    ("J-1042", "2026-06-30", (
        ("5000", "Year end accrual - cost of sales", "15000.00", "0.00"),
        ("6000", "Year end accrual - operating costs", "4200.00", "0.00"),
        ("2000", "Year end accruals payable", "0.00", "19200.00"),
    )),
)

# ---------------------------------------------------------------------------
# Client B: a second clean ledger, different entity and different file format
# ---------------------------------------------------------------------------

ENTITY_B = "BH01"
CURRENCY_B = "AUD"
SOURCE_SYSTEM_B = "FINLEDGER"

ACCOUNT_NAMES_B = {
    "10100": "Bank Current Account",
    "11200": "Accounts Receivable",
    "20100": "Accounts Payable",
    "30500": "Share Capital",
    "40000": "Service Revenue",
    "51000": "Direct Costs",
    "60500": "Administrative Expenses",
}

JOURNALS_B = (
    ("BH-2001", "2025-08-11", (
        ("11200", "Service fees invoiced - engagement 1", "62000.00", "0.00"),
        ("11200", "Service fees invoiced - engagement 2", "18500.00", "0.00"),
        ("40000", "Service revenue - August", "0.00", "80500.00"),
    )),
    ("BH-2002", "2025-09-26", (
        ("51000", "Subcontractor costs - engagement 1", "24000.00", "0.00"),
        ("51000", "Subcontractor costs - engagement 2", "6750.00", "0.00"),
        ("60500", "Software subscriptions - September", "1500.00", "0.00"),
        ("20100", "Supplier invoices payable - September", "0.00", "32250.00"),
    )),
    ("BH-2003", "2025-11-14", (
        ("10100", "Customer receipt - engagement 1", "45000.00", "0.00"),
        ("10100", "Customer receipt - engagement 2", "17000.00", "0.00"),
        ("11200", "Receivables settled - November", "0.00", "62000.00"),
    )),
    ("BH-2004", "2026-01-09", (
        ("60500", "Premises costs - January", "9400.00", "0.00"),
        ("60500", "Telecommunications - January", "3150.00", "0.00"),
        ("60500", "Bank charges - January", "1275.00", "0.00"),
        ("10100", "Administrative expenses settled", "0.00", "13825.00"),
    )),
    ("BH-2005", "2026-03-05", (
        ("20100", "Supplier payment - run 1", "15000.00", "0.00"),
        ("20100", "Supplier payment - run 2", "7500.00", "0.00"),
        ("10100", "Bank payment to suppliers", "0.00", "22500.00"),
    )),
    ("BH-2006", "2026-05-21", (
        ("10100", "Share capital subscribed", "50000.00", "0.00"),
        ("11200", "Service fees invoiced - engagement 3", "9800.00", "0.00"),
        ("30500", "Share capital issued", "0.00", "50000.00"),
        ("40000", "Service revenue - May", "0.00", "9800.00"),
    )),
)


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


def build_gl(journals, account_names, entity, period, currency, source_system):
    """Expand the journal specification into canonical General Ledger lines.

    line_no restarts at 1 within each journal.
    """
    lines = []
    for journal_id, posting_date, entries in journals:
        for line_no, (code, description, debit, credit) in enumerate(entries, start=1):
            lines.append({
                "entity": entity,
                "account_code": code,
                "account_name": account_names[code],
                "posting_date": as_date(posting_date),
                "period": period,
                "journal_id": journal_id,
                "line_no": line_no,
                "description": description,
                "debit": money(debit),
                "credit": money(credit),
                "currency": currency,
                "source_system": source_system,
            })
    return lines


def build_tb(gl_lines, chart, entity):
    """Derive the Trial Balance from the General Ledger.

    closing = opening + period debits - period credits, applied identically to
    every account. Credit-balance accounts therefore close negative, which is
    intended. Accounts with no General Ledger movement are still included.
    """
    debits, credits = account_totals(gl_lines)
    rows = []
    for code, name, _type, opening in chart:
        op = money(opening)
        dr = money(debits[code])
        cr = money(credits[code])
        rows.append({
            "entity": entity,
            "account_code": code,
            "account_name": name,
            "opening_balance": op,
            "period_debits": dr,
            "period_credits": cr,
            "closing_balance": money(op + dr - cr),
        })
    return rows


def account_totals(gl_lines):
    """Total debits and total credits per account code."""
    debits = defaultdict(lambda: ZERO)
    credits = defaultdict(lambda: ZERO)
    for line in gl_lines:
        debits[line["account_code"]] += line["debit"]
        credits[line["account_code"]] += line["credit"]
    return debits, credits


def journal_differences(gl_lines):
    """Debits minus credits per journal. Zero everywhere in a clean ledger."""
    debits = defaultdict(lambda: ZERO)
    credits = defaultdict(lambda: ZERO)
    for line in gl_lines:
        debits[line["journal_id"]] += line["debit"]
        credits[line["journal_id"]] += line["credit"]
    ids = set(debits) | set(credits)
    return {jid: money(debits[jid] - credits[jid]) for jid in sorted(ids)}


def gl_derived_closing(gl_lines, chart):
    """The closing balance the General Ledger implies, per account.

    Computed over every General Ledger line regardless of posting date. A line
    posted outside the financial year is a cutoff finding for C4, not a balance
    difference for C2, and restricting this total by date would turn one defect
    into two.
    """
    debits, credits = account_totals(gl_lines)
    return {
        code: money(money(opening) + debits[code] - credits[code])
        for code, _name, _type, opening in chart
    }


def dedupe_key(line):
    """The exact-duplicate key: all eight identifying fields of a line."""
    return (
        line["entity"],
        line["account_code"],
        line["posting_date"],
        line["journal_id"],
        line["line_no"],
        line["debit"],
        line["credit"],
        line["description"],
    )


def exact_duplicate_groups(gl_lines):
    """Keys appearing more than once, with their occurrence count."""
    counts = Counter(dedupe_key(line) for line in gl_lines)
    return {key: n for key, n in counts.items() if n > 1}


def suspected_duplicate_groups(gl_lines):
    """Same account, date and amount under two or more journal identifiers."""
    buckets = defaultdict(set)
    for line in gl_lines:
        key = (
            line["entity"],
            line["account_code"],
            line["posting_date"],
            line["debit"],
            line["credit"],
        )
        buckets[key].add(line["journal_id"])
    return {key: jids for key, jids in buckets.items() if len(jids) > 1}


def out_of_period(gl_lines, fy_start, fy_end):
    return [
        line for line in gl_lines
        if not (fy_start <= line["posting_date"] <= fy_end)
    ]


# ---------------------------------------------------------------------------
# Defect injection
# ---------------------------------------------------------------------------


def inject_defects(gl_lines, tb_rows, account_names, period):
    """Inject the five defects into the verified clean data.

    Returns the handles later needed to quantify each one. The clean General
    Ledger and the clean Trial Balance are both built and verified before this
    runs, so every difference the manifest records is attributable.
    """
    # D1 -- an unmatched debit appended to the year-end journal. The journal
    # stops self-balancing (C1) and the General Ledger overstates account 5000
    # against the Trial Balance by the same amount (C2).
    j1042 = [line for line in gl_lines if line["journal_id"] == "J-1042"]
    d1_line = {
        "entity": ENTITY_A,
        "account_code": "5000",
        "account_name": account_names["5000"],
        "posting_date": j1042[0]["posting_date"],
        "period": period,
        "journal_id": "J-1042",
        "line_no": max(line["line_no"] for line in j1042) + 1,
        "description": "Unposted accrual adjustment",
        "debit": money("250.00"),
        "credit": ZERO,
        "currency": CURRENCY_A,
        "source_system": SOURCE_SYSTEM_A,
    }
    gl_lines.append(d1_line)

    # D2 -- an exact copy of the 1200.00 operating expense, reusing the same
    # line_no so all eight fields of the duplicate key match. DEDUPE reports
    # the pair; the Trial Balance was derived before the copy existed, so the
    # same amount also shows as a General Ledger to Trial Balance difference.
    d2_source = next(
        line for line in gl_lines
        if line["journal_id"] == "J-1015"
        and line["account_code"] == "6000"
        and line["debit"] == money("1200.00")
    )
    d2_line = dict(d2_source)
    gl_lines.append(d2_line)

    # D3 -- the Trial Balance closing balance of account 4100 moved by 1850.00
    # with the movement columns left alone. The Trial Balance now disagrees
    # with the General Ledger (C2) and no longer sums to zero (C6).
    d3_row = next(row for row in tb_rows if row["account_code"] == "4100")
    d3_row["closing_balance"] = money(d3_row["closing_balance"] + money("1850.00"))

    # D4 -- a posting dated into the prior financial year, amount unchanged.
    d4_line = min(
        (line for line in gl_lines if line["journal_id"] == "J-1003"),
        key=lambda line: line["line_no"],
    )
    d4_line["posting_date"] = date(2025, 6, 28)

    # D5 -- no injection. Account 1500 already holds an opening balance and no
    # General Ledger activity, which is the completeness break C3 tests.

    return {"d1": d1_line, "d2": d2_line, "d3": d3_row, "d4": d4_line}


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------


def build_manifest(gl_lines, tb_rows, chart, handles, cfg, tolerance, fy_start, fy_end):
    """Record every injected defect and the exceptions it is expected to raise.

    Every value is measured from the final data rather than restated from the
    defect table, so the manifest cannot drift away from the CSVs it describes.
    """
    differences = journal_differences(gl_lines)
    derived = gl_derived_closing(gl_lines, chart)
    tb_by_code = {row["account_code"]: row for row in tb_rows}

    def c2_difference(code):
        return money(abs(derived[code] - tb_by_code[code]["closing_balance"]))

    def line_amount(line):
        return money(line["debit"] + line["credit"])

    tolerance_str = dec_str(tolerance)

    d1_c1 = money(abs(differences["J-1042"]))
    d1_c2 = c2_difference("5000")
    d2_value = line_amount(handles["d2"])
    d2_c1 = money(abs(differences["J-1015"]))
    d2_c2 = c2_difference("6000")
    d3_c2 = c2_difference("4100")
    d3_c6 = money(abs(sum((row["closing_balance"] for row in tb_rows), ZERO)))
    d4_value = line_amount(handles["d4"])
    d5_value = money(abs(tb_by_code["1500"]["closing_balance"]))

    def exception(control_id, journal_id, account_code, value):
        return {
            "control_id": control_id,
            "journal_id": journal_id,
            "account_code": account_code,
            "value": dec_str(value),
            "above_materiality": value > tolerance,
        }

    defects = [
        {
            "id": "D1",
            "description": (
                f"A line for account 5000 Cost of Sales, debit {dec_str(handles['d1']['debit'])}, "
                "appended to year-end journal J-1042 with no matching credit. The journal no "
                "longer self-balances (C1), and the General Ledger movement on 5000 now exceeds "
                "the Trial Balance by the same amount (C2). Both sit below the "
                f"{tolerance_str} materiality tolerance and are recorded as immaterial."
            ),
            "injected_into": "gl",
            "expected_exceptions": [
                exception("C1", "J-1042", None, d1_c1),
                exception("C2", None, "5000", d1_c2),
            ],
        },
        {
            "id": "D2",
            "description": (
                "An exact copy of J-1015 line 1, account 6000 Operating Expenses, debit "
                f"{dec_str(handles['d2']['debit'])}, appended to the General Ledger reusing the "
                "same line_no so all eight fields of the duplicate key match. One root cause, "
                "three controls. DEDUPE reports the pair. Because the copy is a one-sided debit, "
                f"journal J-1015 is out of balance by {dec_str(d2_c1)} in the file as delivered "
                "and C1 reports it: stage 3 records a reversible duplicate decision and returns "
                "the population unchanged, so removing a row never silently changes what another "
                "control sees. The Trial Balance was derived before the copy existed, so the same "
                "amount also appears as a General Ledger to Trial Balance difference on 6000 (C2)."
            ),
            "injected_into": "gl",
            "expected_exceptions": [
                exception("DEDUPE", "J-1015", "6000", d2_value),
                exception("C1", "J-1015", None, d2_c1),
                exception("C2", None, "6000", d2_c2),
            ],
        },
        {
            "id": "D3",
            "description": (
                "1850.00 added to the Trial Balance closing balance of account 4100 Sales "
                "Revenue, leaving period_debits and period_credits unchanged. The Trial Balance "
                "no longer agrees with the balance derived from the General Ledger for that "
                "account (C2) and no longer sums to zero (C6). The General Ledger is untouched."
            ),
            "injected_into": "tb",
            "expected_exceptions": [
                exception("C2", None, "4100", d3_c2),
                exception("C6", None, None, d3_c6),
            ],
        },
        {
            "id": "D4",
            "description": (
                "The posting date of J-1003 line 1, account 1000 Cash at Bank, debit "
                f"{dec_str(d4_value)}, moved to "
                f"{handles['d4']['posting_date'].isoformat()}, outside "
                f"{fy_start.isoformat()} to {fy_end.isoformat()}, with the amount unchanged (C4). "
                "The recorded value is the amount of the misdated posting. No balance difference "
                "arises: C2 compares full-year account totals, so a line in the wrong period is "
                "still counted in the account it was posted to."
            ),
            "injected_into": "gl",
            "expected_exceptions": [
                exception("C4", "J-1003", "1000", d4_value),
            ],
        },
        {
            "id": "D5",
            "description": (
                "No injection required. Account 1500 Inventory carries an opening balance of "
                f"{dec_str(tb_by_code['1500']['opening_balance'])} and receives no General Ledger "
                "lines at all, so it is present in the Trial Balance and absent from the General "
                "Ledger (C3). The recorded value is the account's Trial Balance closing balance, "
                "the balance one source carries and the other has no record of. C2 raises nothing "
                "on 1500: with no movement in either source, opening plus nil equals closing."
            ),
            "injected_into": "none",
            "expected_exceptions": [
                exception("C3", None, "1500", d5_value),
            ],
        },
    ]

    generated_at = (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )

    return {
        "generated_at": generated_at,
        "client_id": cfg["client_id"],
        "period": cfg["period"],
        "materiality_tolerance": tolerance_str,
        "defects": defects,
        "expected_exception_count": sum(len(d["expected_exceptions"]) for d in defects),
    }


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

CLIENT_A_GL_HEADER = [
    "Co Code", "Nominal Code", "Nominal Name", "Posting Dt", "Per", "Jnl Ref",
    "Ln", "Narrative", "Amount DR", "Amount CR", "Ccy", "Src",
]

CLIENT_A_TB_HEADER = [
    "Co Code", "Nominal Code", "Nominal Name", "Op Bal", "Per DR", "Per CR", "Cl Bal",
]

CLIENT_B_GL_HEADER = [
    "Entity_ID", "GL_Acct", "GL_Acct_Desc", "TransDate", "FiscalPeriod", "Batch_ID",
    "LineNum", "Description", "Debit_Amt", "Credit_Amt", "Currency", "SourceSystem",
]


def client_a_gl_rows(gl_lines):
    return [
        [
            line["entity"],
            line["account_code"],
            line["account_name"],
            line["posting_date"].strftime("%Y-%m-%d"),
            line["period"],
            line["journal_id"],
            line["line_no"],
            line["description"],
            dec_str(line["debit"]),
            dec_str(line["credit"]),
            line["currency"],
            line["source_system"],
        ]
        for line in gl_lines
    ]


def client_a_tb_rows(tb_rows):
    return [
        [
            row["entity"],
            row["account_code"],
            row["account_name"],
            dec_str(row["opening_balance"]),
            dec_str(row["period_debits"]),
            dec_str(row["period_credits"]),
            dec_str(row["closing_balance"]),
        ]
        for row in tb_rows
    ]


def client_b_gl_rows(gl_lines):
    return [
        [
            line["entity"],
            line["account_code"],
            line["account_name"],
            line["posting_date"].strftime("%d/%m/%Y"),
            line["period"],
            line["journal_id"],
            line["line_no"],
            line["description"],
            dec_str(line["debit"]),
            dec_str(line["credit"]),
            line["currency"],
            line["source_system"],
        ]
        for line in gl_lines
    ]


def write_csv(path, header, rows):
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(header)
        writer.writerows(rows)
    print(f"      wrote {path.relative_to(REPO_ROOT)} ({len(rows)} rows)")


def write_json(path, payload):
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
        handle.write("\n")
    print(f"      wrote {path.relative_to(REPO_ROOT)}")


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def generate():
    cfg = yaml.safe_load((CONFIG_DIR / "client_a.yaml").read_text(encoding="utf-8"))

    # Tolerance and period dates are engagement configuration, never constants
    # in this file.
    tolerance = money(cfg["materiality"]["tolerance"])
    fy_start = as_date(cfg["financial_year"]["start"])
    fy_end = as_date(cfg["financial_year"]["end"])
    period = cfg["period"]

    account_names_a = {code: name for code, name, _type, _open in CHART_OF_ACCOUNTS_A}

    print(f"Client {cfg['client_id']}, period {period}, "
          f"financial year {fy_start.isoformat()} to {fy_end.isoformat()}, "
          f"materiality tolerance {dec_str(tolerance)} {cfg['materiality']['currency']}")

    # -- clean data ---------------------------------------------------------
    gl = build_gl(JOURNALS_A, account_names_a, ENTITY_A, period, CURRENCY_A, SOURCE_SYSTEM_A)
    tb = build_tb(gl, CHART_OF_ACCOUNTS_A, ENTITY_A)

    heading(f"Clean data: {len(gl)} General Ledger lines across "
            f"{len({line['journal_id'] for line in gl})} journals, {len(tb)} Trial Balance rows")

    differences = journal_differences(gl)
    unbalanced = {jid: diff for jid, diff in differences.items() if diff != ZERO}
    check("V1", "every journal balances: debits equal credits within each journal_id",
          not unbalanced, "no unbalanced journals", unbalanced or "none")

    total_debits = money(sum((line["debit"] for line in gl), ZERO))
    total_credits = money(sum((line["credit"] for line in gl), ZERO))
    check("V2", f"whole ledger balances: total debits {dec_str(total_debits)} "
                f"equal total credits {dec_str(total_credits)}",
          total_debits == total_credits, dec_str(total_debits), dec_str(total_credits))

    tb_sum = money(sum((row["closing_balance"] for row in tb), ZERO))
    check("V3", "Trial Balance closing balances sum to exactly 0.00",
          tb_sum == ZERO, "0.00", dec_str(tb_sum))

    rollforward_breaks = {
        row["account_code"]: (
            dec_str(money(row["opening_balance"] + row["period_debits"] - row["period_credits"])),
            dec_str(row["closing_balance"]),
        )
        for row in tb
        if money(row["opening_balance"] + row["period_debits"] - row["period_credits"])
        != row["closing_balance"]
    }
    check("V4", "every account: opening + period debits - period credits equals closing",
          not rollforward_breaks, "no breaks", rollforward_breaks or "none")

    lines_1500 = [line for line in gl if line["account_code"] == "1500"]
    check("V5", "account 1500 Inventory has zero General Ledger lines",
          not lines_1500, 0, len(lines_1500))

    _debits, credits_by_account = account_totals(gl)
    revenue = money(credits_by_account["4100"])
    floor = money("300000.00")
    check("V6", f"account 4100 total credits {dec_str(revenue)} are at least {dec_str(floor)}",
          revenue >= floor, f">= {dec_str(floor)}", dec_str(revenue))

    outside = out_of_period(gl, fy_start, fy_end)
    check("V7", f"every posting date falls inside {fy_start.isoformat()} "
                f"to {fy_end.isoformat()}",
          not outside, 0,
          [line["posting_date"].isoformat() for line in outside] or 0)

    # -- injection ----------------------------------------------------------
    handles = inject_defects(gl, tb, account_names_a, period)

    heading(f"After injection: {len(gl)} General Ledger lines, {len(tb)} Trial Balance rows")

    unbalanced_after = {
        jid: diff for jid, diff in journal_differences(gl).items() if diff != ZERO
    }
    expected_v8 = {"J-1015": money("1200.00"), "J-1042": money("250.00")}
    check("V8", "exactly two journals fail to balance in the General Ledger as received: "
                "J-1042 by 250.00 and J-1015 by 1200.00",
          unbalanced_after == expected_v8,
          {k: dec_str(v) for k, v in sorted(expected_v8.items())},
          {k: dec_str(v) for k, v in sorted(unbalanced_after.items())})
    note("Stage 3 records a reversible duplicate decision and returns the population\n"
         "unchanged, so stage 4 reads the General Ledger exactly as delivered. J-1015 is\n"
         "out of balance because D2's duplicate is a one-sided debit, and that one root\n"
         "cause is reported by C1, DEDUPE and C2 alike.")

    duplicate_groups = exact_duplicate_groups(gl)
    dup_ok = (
        len(duplicate_groups) == 1
        and next(iter(duplicate_groups.values())) == 2
        and handles["d2"]["account_code"] == "6000"
        and handles["d2"]["journal_id"] == "J-1015"
        and handles["d2"]["debit"] == money("1200.00")
    )
    check("V9", "exactly one exact-duplicate group: account 6000, journal J-1015, debit 1200.00",
          dup_ok,
          "1 group of 2 on (6000, J-1015, 1200.00)",
          {"groups": len(duplicate_groups),
           "sizes": sorted(duplicate_groups.values()),
           "account_code": handles["d2"]["account_code"],
           "journal_id": handles["d2"]["journal_id"],
           "debit": dec_str(handles["d2"]["debit"])})

    tb_sum_after = money(sum((row["closing_balance"] for row in tb), ZERO))
    check("V10", "Trial Balance closing balances now sum to 1850.00",
          tb_sum_after == money("1850.00"), "1850.00", dec_str(tb_sum_after))

    outside_after = out_of_period(gl, fy_start, fy_end)
    outside_dates = [line["posting_date"].isoformat() for line in outside_after]
    check("V11", "exactly one posting date falls outside the financial year: 2025-06-28",
          outside_dates == ["2025-06-28"], ["2025-06-28"], outside_dates)

    gl_accounts = {line["account_code"] for line in gl}
    tb_accounts = {row["account_code"] for row in tb}
    check("V12", "account 1500 is in the Trial Balance and absent from the General Ledger",
          "1500" in tb_accounts and "1500" not in gl_accounts,
          "in Trial Balance, not in General Ledger",
          {"in_tb": "1500" in tb_accounts, "in_gl": "1500" in gl_accounts})

    # -- manifest -----------------------------------------------------------
    manifest = build_manifest(gl, tb, CHART_OF_ACCOUNTS_A, handles, cfg,
                              tolerance, fy_start, fy_end)

    heading("Manifest")

    defect_count = len(manifest["defects"])
    exception_count = manifest["expected_exception_count"]
    check("V13", "the manifest lists exactly 9 expected exceptions across 5 defects",
          exception_count == 9 and defect_count == 5,
          {"defects": 5, "expected_exceptions": 9},
          {"defects": defect_count, "expected_exceptions": exception_count})

    # -- guards on the oracle ----------------------------------------------
    #
    # V1 to V13 are the properties the task names. These are the ones that keep
    # the manifest honest: each protects a claim the expected exception count
    # depends on.
    heading("Extra guards on the manifest")

    opening_sum = money(sum((money(opening) for _c, _n, _t, opening in CHART_OF_ACCOUNTS_A), ZERO))
    check("G1", "chart of accounts opening balances sum to 0.00",
          opening_sum == ZERO, "0.00", dec_str(opening_sum))

    clean_gl_again = build_gl(JOURNALS_A, account_names_a, ENTITY_A, period,
                              CURRENCY_A, SOURCE_SYSTEM_A)
    clean_duplicates = exact_duplicate_groups(clean_gl_again)
    check("G2", "the clean General Ledger contains no exact duplicates, so V9's group is D2's",
          not clean_duplicates, 0, len(clean_duplicates))

    suspected = suspected_duplicate_groups(gl)
    check("G3", "no suspected duplicates: no account, date and amount recur across journals",
          not suspected, 0, len(suspected))

    missing_from_gl = sorted(tb_accounts - gl_accounts)
    missing_from_tb = sorted(gl_accounts - tb_accounts)
    check("G4", "account 1500 is the only completeness break, so C3 has exactly one finding",
          missing_from_gl == ["1500"] and not missing_from_tb,
          {"in_tb_not_gl": ["1500"], "in_gl_not_tb": []},
          {"in_tb_not_gl": missing_from_gl, "in_gl_not_tb": missing_from_tb})

    derived = gl_derived_closing(gl, CHART_OF_ACCOUNTS_A)
    tb_by_code = {row["account_code"]: row for row in tb}
    c2_breaks = sorted(
        code for code in tb_by_code
        if derived[code] != tb_by_code[code]["closing_balance"]
    )
    check("G5", "exactly three accounts disagree between General Ledger and Trial Balance",
          c2_breaks == ["4100", "5000", "6000"], ["4100", "5000", "6000"], c2_breaks)

    manifest_c2 = sorted(
        exc["account_code"]
        for defect in manifest["defects"]
        for exc in defect["expected_exceptions"]
        if exc["control_id"] == "C2"
    )
    check("G6", "the C2 exceptions in the manifest are exactly the accounts that disagree",
          manifest_c2 == c2_breaks, c2_breaks, manifest_c2)

    # The mirror of G6 for C1. Its absence is what let the manifest claim one
    # unbalanced journal while the delivered file held two.
    manifest_c1 = sorted(
        exc["journal_id"]
        for defect in manifest["defects"]
        for exc in defect["expected_exceptions"]
        if exc["control_id"] == "C1"
    )
    check("G7", "the C1 exceptions in the manifest are exactly the journals out of balance",
          manifest_c1 == sorted(unbalanced_after), sorted(unbalanced_after), manifest_c1)

    above = sum(
        1 for defect in manifest["defects"]
        for exc in defect["expected_exceptions"]
        if exc["above_materiality"]
    )
    below = exception_count - above
    # Tied to the tolerance currently in config. Change that and the expected
    # exception set changes with it, which this guard will say out loud.
    check("G8", f"the defects split 7 above and 2 below the {dec_str(tolerance)} tolerance",
          (above, below) == (7, 2),
          "7 above materiality, 2 below",
          f"{above} above, {below} below")

    # -- Client B -----------------------------------------------------------
    cfg_b = yaml.safe_load((CONFIG_DIR / "client_b.yaml").read_text(encoding="utf-8"))
    fy_start_b = as_date(cfg_b["financial_year"]["start"])
    fy_end_b = as_date(cfg_b["financial_year"]["end"])
    gl_b = build_gl(JOURNALS_B, ACCOUNT_NAMES_B, ENTITY_B, cfg_b["period"],
                    CURRENCY_B, SOURCE_SYSTEM_B)

    heading(f"Client {cfg_b['client_id']}: {len(gl_b)} General Ledger lines across "
            f"{len({line['journal_id'] for line in gl_b})} journals, no defects")

    unbalanced_b = {
        jid: dec_str(diff) for jid, diff in journal_differences(gl_b).items() if diff != ZERO
    }
    check("G9", "every journal balances", not unbalanced_b, "none", unbalanced_b or "none")

    two_sided = [
        line for line in gl_b
        if not ((line["debit"] > ZERO and line["credit"] == ZERO)
                or (line["credit"] > ZERO and line["debit"] == ZERO))
    ]
    check("G10", "every line carries a positive debit or a positive credit, never both",
          not two_sided, 0, len(two_sided))

    outside_b = out_of_period(gl_b, fy_start_b, fy_end_b)
    check("G11", f"every posting date falls inside {fy_start_b.isoformat()} "
                 f"to {fy_end_b.isoformat()}",
          not outside_b, 0, len(outside_b))

    duplicates_b = exact_duplicate_groups(gl_b)
    suspected_b = suspected_duplicate_groups(gl_b)
    check("G12", "no exact and no suspected duplicates",
          not duplicates_b and not suspected_b, 0,
          {"exact": len(duplicates_b), "suspected": len(suspected_b)})

    # -- write --------------------------------------------------------------
    # Nothing has been written until here. Every assertion has passed.
    heading("Output")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    write_csv(DATA_DIR / "client_a_gl.csv", CLIENT_A_GL_HEADER, client_a_gl_rows(gl))
    write_csv(DATA_DIR / "client_a_tb.csv", CLIENT_A_TB_HEADER, client_a_tb_rows(tb))
    write_csv(DATA_DIR / "client_b_gl.csv", CLIENT_B_GL_HEADER, client_b_gl_rows(gl_b))
    write_json(DATA_DIR / "defects_manifest.json", manifest)


def main() -> int:
    try:
        generate()
    except VerificationFailure as failure:
        print(f"\nFAIL  {failure.ident}   {failure.description}")
        print(f"          expected: {failure.expected}")
        print(f"          actual:   {failure.actual}")
        print("\nNo files written. The generated data is left untouched.")
        print("The failure is reported rather than corrected: adjusting the data until "
              "the assertion passes would destroy the only thing this file is for.")
        return 1
    print("\nAll assertions passed. Four files written under data/.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
