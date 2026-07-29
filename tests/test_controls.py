"""Control tests. Phase 3.

Two kinds of test live here, and the difference between them is load-bearing.

Unit tests build their population in memory, a handful of rows at a time, each
shaped to defeat exactly one control. They must not read the dataset in data/,
for two reasons. A control tested against a file is only ever tested against the
defects that file happens to carry, so a control that never fires would be
indistinguishable from one that is broken; and a fixture written in the test is
the only way to state, in the test itself, what the control is supposed to find
and what it is supposed to leave alone.

Integration tests read data/ because the thing under test is the real pipeline
over the real dataset: that the rows arriving are the rows accounted for, that
none of them is quarantined, that the register the run produces is the register
the manifest claims, and that stage 1 completes with no model attached.

The manifest is the oracle for the register. Where the two disagree the test
fails; it does not adjust either side.

Nothing here writes to data/, config/ or registry/. The registry ships empty by
design, so the run that reaches the controls approves a copy of it in a
temporary directory, exactly as a person would approve the original.
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from src import approve, run
from src.models import CanonicalGLLine, CanonicalTBRow, parse_money
from src.s0_ingest import read_rows, register_source
from src.s1_mapping import (
    StubMappingLLM,
    load_registry,
    load_synonyms,
    resolve_columns,
    save_registry,
)
from src.s2_validate import ControlTotalError, assert_control_total
from src.s3_dedupe import find_duplicates
from src.s4_reconcile import (
    control_c1,
    control_c2,
    control_c3,
    control_c4,
    control_c5,
    control_c6,
)
from src.s7_report import REVIEW_QUEUE_FILE, counters

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
CONFIG_PATH = REPO_ROOT / "config" / "client_a.yaml"
SYNONYMS_PATH = REPO_ROOT / "config" / "synonyms.yaml"
REGISTRY_PATH = REPO_ROOT / "registry" / "mappings.json"
MANIFEST_PATH = DATA_DIR / "defects_manifest.json"

PROVIDER = "CLIENT_A"
ENTITY = "AU01"

# Supplied to every control the way the driver supplies it, from the caller and
# never from inside the control. 1,000.00 is the figure the fixtures below are
# written against; no fixture's outcome turns on where the threshold sits, only
# on how each finding is graded.
TOLERANCE = Decimal("1000.00")
FINANCIAL_YEAR = (date(2025, 7, 1), date(2026, 6, 30))

# Records produced outside a registered run carry an identifier saying so.
RUN_ID = "TEST-RUN"

# The moment the temporary approval was recorded. Fixed rather than taken from
# the clock, so the registry a test builds is the same registry every time.
APPROVED_AT = "2026-01-01T00:00:00Z"


# ---------------------------------------------------------------------------
# Building a population
#
# Every field a canonical record requires is supplied. The defaults are the
# ordinary values a ledger carries, so what a fixture states explicitly is
# exactly the defect it was written to plant.
# ---------------------------------------------------------------------------


def gl_line(
    account_code: str,
    journal_id: str,
    line_no: int,
    *,
    debit: str = "0.00",
    credit: str = "0.00",
    posting_date: date = date(2025, 8, 1),
    description: str = "",
    account_name: str = "Account",
) -> CanonicalGLLine:
    """One General Ledger line."""
    return CanonicalGLLine(
        entity=ENTITY,
        account_code=account_code,
        account_name=account_name,
        posting_date=posting_date,
        period="FY2026",
        journal_id=journal_id,
        line_no=line_no,
        description=description,
        debit=Decimal(debit),
        credit=Decimal(credit),
        currency="AUD",
        source_system="ERP-X",
    )


def tb_row(
    account_code: str,
    *,
    opening: str = "0.00",
    debits: str = "0.00",
    credits: str = "0.00",
    closing: str = "0.00",
    account_name: str = "Account",
) -> CanonicalTBRow:
    """One Trial Balance row, carried exactly as stated and never re-derived."""
    return CanonicalTBRow(
        entity=ENTITY,
        account_code=account_code,
        account_name=account_name,
        opening_balance=Decimal(opening),
        period_debits=Decimal(debits),
        period_credits=Decimal(credits),
        closing_balance=Decimal(closing),
    )


# ---------------------------------------------------------------------------
# Fixtures: one population per control, each shaped to defeat that control
# ---------------------------------------------------------------------------


@pytest.fixture
def balanced_journal() -> list[CanonicalGLLine]:
    """J-001: 500.00 debited and 500.00 credited. It self-balances."""
    return [
        gl_line("1000", "J-001", 1, debit="500.00", posting_date=date(2025, 8, 1)),
        gl_line("4000", "J-001", 2, credit="500.00", posting_date=date(2025, 8, 1)),
    ]


@pytest.fixture
def unbalanced_ledger(balanced_journal: list[CanonicalGLLine]) -> list[CanonicalGLLine]:
    """The balanced journal, and J-002 debited 600.00 against 500.00 credited."""
    return balanced_journal + [
        gl_line("1000", "J-002", 1, debit="600.00", posting_date=date(2025, 8, 2)),
        gl_line("4000", "J-002", 2, credit="500.00", posting_date=date(2025, 8, 2)),
    ]


@pytest.fixture
def ledger_to_balance_difference() -> tuple[list[CanonicalGLLine], list[CanonicalTBRow]]:
    """A ledger deriving 500.00 on account 1000 against a stated 400.00."""
    return (
        [gl_line("1000", "J-001", 1, debit="500.00")],
        [tb_row("1000", opening="0.00", debits="500.00", closing="400.00")],
    )


@pytest.fixture
def dormant_account() -> tuple[list[CanonicalGLLine], list[CanonicalTBRow]]:
    """Account 1500 carries an opening balance and receives no ledger line.

    The boundary between C2 and C3. The account is present in one source and
    absent from the other, which is C3's finding; its opening balance plus no
    movement equals its closing balance, so there is nothing for C2 to report.
    """
    return (
        [gl_line("1000", "J-001", 1, debit="500.00")],
        [
            tb_row("1000", opening="0.00", debits="500.00", closing="500.00"),
            tb_row(
                "1500",
                opening="95000.00",
                closing="95000.00",
                account_name="Inventory",
            ),
        ],
    )


@pytest.fixture
def one_sided_account() -> tuple[list[CanonicalGLLine], list[CanonicalTBRow]]:
    """Account 9999 is in the Trial Balance and the ledger has never heard of it."""
    return (
        [gl_line("1000", "J-001", 1, debit="500.00")],
        [
            tb_row("1000", opening="0.00", debits="500.00", closing="500.00"),
            tb_row(
                "9999",
                credits="750.00",
                closing="-750.00",
                account_name="Suspense",
            ),
        ],
    )


@pytest.fixture
def misdated_posting() -> list[CanonicalGLLine]:
    """J-001 line 1 is dated three days before the financial year opens.

    Line 2 is dated inside the year and is here so the journal self-balances:
    the population carries one defect, and C1 has nothing to say about it.
    """
    return [
        gl_line("1000", "J-001", 1, debit="500.00", posting_date=date(2025, 6, 28)),
        gl_line("4000", "J-001", 2, credit="500.00", posting_date=date(2025, 8, 1)),
    ]


@pytest.fixture
def rollforward_break() -> tuple[list[CanonicalTBRow], list[CanonicalTBRow]]:
    """Account 1000 closed at 1,000.00 last period and opens at 900.00 this one."""
    current = [tb_row("1000", opening="900.00", closing="900.00")]
    prior = [tb_row("1000", closing="1000.00")]
    return current, prior


@pytest.fixture
def internally_unbalanced_trial_balance() -> list[CanonicalTBRow]:
    """A Trial Balance failing both of C6's limbs, by the same 100.00.

    Account 4000's closing balance does not follow from its own opening and
    movement, and the closing balances sum to 100.00 rather than nil. One
    condition showing itself two ways, and one amount of money.
    """
    return [
        tb_row("1000", opening="500.00", closing="500.00"),
        tb_row("4000", opening="-500.00", closing="-400.00"),
    ]


@pytest.fixture
def duplicated_ledger() -> list[CanonicalGLLine]:
    """J-001 line 1 posted twice, identical across all eight key fields."""
    fee = dict(debit="1200.00", description="Monthly fee", account_name="Operating Expenses")
    return [
        gl_line("6000", "J-001", 1, **fee),
        gl_line("6000", "J-001", 1, **fee),
        gl_line("1000", "J-001", 2, credit="2400.00"),
    ]


# ---------------------------------------------------------------------------
# Fixtures: the real thing
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def client_config() -> dict:
    """The provider's engagement configuration, read and never written."""
    return yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def manifest() -> dict:
    """The planted defects and the exceptions each is expected to produce."""
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def warm_run(tmp_path_factory: pytest.TempPathFactory) -> run.RunOutcome:
    """A full run of the provider, carried through to the controls.

    The registry ships empty, so the first run of a provider cannot settle
    every column on its own and stops for a person before the controls. The
    sequence here is the one a person performs — run, approve, run — carried
    out against a copy of the registry in a temporary directory.

    registry/mappings.json is read once, to take the copy, and never written.
    Nothing under data/ or config/ is touched at all, and every artefact the
    two runs produce is written into the temporary directory.
    """
    workspace = tmp_path_factory.mktemp("provider_run")
    registry_path = workspace / "mappings.json"
    registry_path.write_bytes(REGISTRY_PATH.read_bytes())
    out_dir = workspace / "out"

    with pytest.MonkeyPatch.context() as patched:
        patched.setattr(run, "REGISTRY_PATH", registry_path)

        with pytest.raises(run.ReviewRequired):
            run.reconcile(PROVIDER, out_dir, llm=StubMappingLLM())

        queue = approve.load_queue(out_dir / REVIEW_QUEUE_FILE)
        updated, _written = approve.approve(
            queue,
            load_registry(registry_path),
            columns=None,
            approved_at=APPROVED_AT,
        )
        save_registry(updated, registry_path)

        return run.reconcile(PROVIDER, out_dir, llm=StubMappingLLM())


# ---------------------------------------------------------------------------
# C1 — Journal balance
# ---------------------------------------------------------------------------


def test_c1_fires_on_an_unbalanced_journal(unbalanced_ledger):
    """J-002 is debited 600.00 against 500.00 credited."""
    exceptions, summary = control_c1(unbalanced_ledger, TOLERANCE, RUN_ID)

    assert [(record.journal_id, record.value) for record in exceptions] == [
        ("J-002", Decimal("100.00"))
    ]
    assert summary.status == "RUN"


def test_c1_raises_nothing_on_a_balanced_journal(balanced_journal):
    """J-001's debits equal its credits, so there is nothing to report."""
    exceptions, summary = control_c1(balanced_journal, TOLERANCE, RUN_ID)

    assert exceptions == []
    # A control that found nothing is still visible as having run.
    assert (summary.status, summary.rows_tested) == ("RUN", 2)


# ---------------------------------------------------------------------------
# C2 — General Ledger to Trial Balance agreement
# ---------------------------------------------------------------------------


def test_c2_fires_on_a_ledger_to_balance_difference(ledger_to_balance_difference):
    """Opening 0.00 plus ledger debits 500.00 against a stated closing 400.00."""
    gl_lines, tb_rows = ledger_to_balance_difference
    exceptions, _summary = control_c2(gl_lines, tb_rows, TOLERANCE, RUN_ID)

    assert [(record.account_code, record.value) for record in exceptions] == [
        ("1000", Decimal("100.00"))
    ]


def test_c2_raises_nothing_on_an_account_with_no_movement(dormant_account):
    """An account with an opening balance and no ledger line reconciles to itself.

    Its absence from the ledger is C3's finding. C2 leaves it alone rather than
    reporting the same absence a second time under a different name.
    """
    gl_lines, tb_rows = dormant_account
    exceptions, _summary = control_c2(gl_lines, tb_rows, TOLERANCE, RUN_ID)

    assert [record.account_code for record in exceptions if record.account_code == "1500"] == []


# ---------------------------------------------------------------------------
# C3 — Account completeness
# ---------------------------------------------------------------------------


def test_c3_fires_on_an_account_present_in_one_source_only(one_sided_account):
    """Account 9999 is in the Trial Balance and carries no ledger line."""
    gl_lines, tb_rows = one_sided_account
    exceptions, _summary = control_c3(gl_lines, tb_rows, TOLERANCE, RUN_ID)

    assert [record.account_code for record in exceptions] == ["9999"]


# ---------------------------------------------------------------------------
# C4 — Period cutoff
# ---------------------------------------------------------------------------


def test_c4_fires_on_a_posting_dated_outside_the_financial_year(misdated_posting):
    """One exception per misdated posting, valued at the amount of that posting.

    A cutoff finding does not change a full-year balance, so the line is still
    counted in the account it was posted to and no other control is disturbed
    by it.
    """
    exceptions, _summary = control_c4(
        misdated_posting, FINANCIAL_YEAR, TOLERANCE, RUN_ID
    )

    assert [
        (record.journal_id, record.account_code, record.value) for record in exceptions
    ] == [("J-001", "1000", Decimal("500.00"))]


# ---------------------------------------------------------------------------
# C5 — Rollforward
# ---------------------------------------------------------------------------


def test_c5_fires_when_a_prior_closing_disagrees_with_the_opening(rollforward_break):
    """Prior closing 1,000.00 against current opening 900.00."""
    current, prior = rollforward_break
    exceptions, _summary = control_c5(current, prior, TOLERANCE, RUN_ID)

    assert [(record.account_code, record.value) for record in exceptions] == [
        ("1000", Decimal("100.00"))
    ]


def test_c5_reports_skipped_when_no_prior_period_is_supplied(rollforward_break):
    """Implemented, not omitted: with nothing to compare it says so and why."""
    current, _prior = rollforward_break
    exceptions, summary = control_c5(current, None, TOLERANCE, RUN_ID)

    assert (summary.status, summary.reason) == ("SKIPPED", "no prior period loaded")
    assert exceptions == []


# ---------------------------------------------------------------------------
# C6 — Trial Balance internal balance
# ---------------------------------------------------------------------------


def test_c6_raises_one_exception_when_both_limbs_fail(
    internally_unbalanced_trial_balance,
):
    """Both limbs fail by 100.00, and 100.00 is one amount of money.

    Two exceptions would count the same money twice under two headings.
    """
    exceptions, _summary = control_c6(
        internally_unbalanced_trial_balance, TOLERANCE, RUN_ID
    )

    assert len(exceptions) == 1, (
        "C6 reported the same imbalance more than once: "
        f"{[str(record.value) for record in exceptions]}"
    )
    assert exceptions[0].value == Decimal("100.00")
    assert "4000" in exceptions[0].evidence


# ---------------------------------------------------------------------------
# DEDUPE — Duplicate detection
# ---------------------------------------------------------------------------


def test_dedupe_fires_on_an_exact_duplicate(duplicated_ledger):
    """Two rows identical across all eight fields of the exact-duplicate key."""
    exceptions, _decisions = find_duplicates(
        duplicated_ledger, tolerance=TOLERANCE, run_id=RUN_ID
    )

    assert [
        (record.journal_id, record.account_code, record.value) for record in exceptions
    ] == [("J-001", "6000", Decimal("1200.00"))]


def test_find_duplicates_returns_the_population_unchanged(duplicated_ledger):
    """Nothing is deleted. The duplicate is flagged and stays in the population.

    Removing it would silently change what another control reports: the copy is
    a one-sided debit, so C1 and C2 both have a finding that depends on it
    still being there.
    """
    before = len(duplicated_ledger)
    delivered = list(duplicated_ledger)

    find_duplicates(duplicated_ledger, tolerance=TOLERANCE, run_id=RUN_ID)

    assert len(duplicated_ledger) == before
    assert duplicated_ledger == delivered


# ---------------------------------------------------------------------------
# The control total
# ---------------------------------------------------------------------------


def test_the_control_total_holds_on_the_real_dataset(warm_run):
    """rows_received == rows_accepted + rows_quarantined, over the whole run."""
    figures = counters(warm_run)

    assert figures["rows_received"] == (
        figures["rows_accepted"] + figures["rows_quarantined"]
    )


def test_a_broken_row_count_raises_control_total_error():
    """Ten rows arrived and nine were accounted for. The run is invalid.

    Raised rather than asserted, so the check cannot be switched off with
    python -O.
    """
    with pytest.raises(ControlTotalError):
        assert_control_total(rows_received=10, rows_accepted=9, rows_quarantined=0)


def test_the_real_dataset_quarantines_no_rows(warm_run):
    """Every planted defect is a finding for a control, not a structural failure.

    A quarantined row would mean a defect never reached the control that exists
    to report it.
    """
    quarantined = [
        entry for dataset in warm_run.datasets for entry in dataset.quarantined
    ]

    assert quarantined == [], (
        "rows were set aside before the controls: "
        f"{[(entry['source_file'], entry['row_number'], entry['rule_id']) for entry in quarantined]}"
    )


# ---------------------------------------------------------------------------
# The golden dataset
# ---------------------------------------------------------------------------


def _claim(record) -> tuple:
    """One exception reduced to what the manifest and the register both state."""
    return (record.control_id, record.journal_id, record.account_code, record.value)


def _expected(entry: dict) -> tuple:
    """One expected exception, its value read by the pipeline's own parser."""
    return (
        entry["control_id"],
        entry["journal_id"],
        entry["account_code"],
        parse_money(entry["value"]),
    )


def _describe(claims: Counter) -> str:
    """Claims in a form a failure can be read from."""
    return "; ".join(
        f"{control_id} journal={journal_id} account={account_code} "
        f"value={value} x{count}"
        for (control_id, journal_id, account_code, value), count in sorted(
            claims.items(), key=lambda item: tuple(str(part) for part in item[0])
        )
    )


def test_a_full_run_produces_the_exceptions_the_manifest_expects(warm_run, manifest):
    """The register against the manifest, as multisets of four-tuples.

    A multiset rather than a set, so a repeated tuple cannot collapse and hide
    a duplicate finding. The manifest is the oracle: where the two disagree
    this fails, and adjusts neither side.
    """
    expected = Counter(
        _expected(entry)
        for defect in manifest["defects"]
        for entry in defect["expected_exceptions"]
    )
    produced = Counter(_claim(record) for record in warm_run.exceptions)

    assert len(warm_run.exceptions) == manifest["expected_exception_count"]
    assert not expected - produced, (
        "the manifest expects exceptions the register did not produce: "
        + _describe(expected - produced)
    )
    assert not produced - expected, (
        "the register produced exceptions the manifest does not claim: "
        + _describe(produced - expected)
    )


# ---------------------------------------------------------------------------
# Stage 1 with no model attached
# ---------------------------------------------------------------------------


def test_resolve_columns_completes_with_no_model(client_config):
    """The pipeline runs correctly with no model attached.

    Tiers 1 and 2 still run, every column the two of them cannot settle enters
    the review queue carrying no tier, and nothing is dropped on the way.
    """
    source = register_source(DATA_DIR / "client_a_gl.csv", client_config)
    headers, rows = read_rows(source)

    mapping, resolved, review = resolve_columns(
        headers,
        rows,
        client_config["client_id"],
        "gl",
        load_registry(REGISTRY_PATH),
        synonyms=load_synonyms(SYNONYMS_PATH),
        gate=client_config["mapping"]["confidence_gate"],
        llm=None,
    )

    accounted = list(mapping) + [proposal.source_column for proposal in review]
    assert sorted(accounted) == sorted(headers)
    assert not [
        proposal for proposal in list(resolved) + list(review)
        if proposal.source_tier == 3
    ]
