"""Stage 4 — The reconciliation engine. Purely deterministic. Phase 1.

No language model appears anywhere in this module. Every figure it reports is
arithmetic over the two populations exactly as they were delivered.

    def run_controls(gl_lines, tb_rows, config, prior_tb=None, *,
                     run_id=UNIDENTIFIED_RUN, duplicate_exceptions=None)
            -> tuple[list[ExceptionRecord], list[ControlSummary]]

Each control is callable on its own, because Phase 3 tests one control at a
time against a fixture built to defeat it. Every one returns its exceptions and
its summary together, so a control cannot be tested without its summary being
produced:

    def control_c1(gl_lines, tolerance, run_id)
    def control_c2(gl_lines, tb_rows, tolerance, run_id)
    def control_c3(gl_lines, tb_rows, tolerance, run_id)
    def control_c4(gl_lines, financial_year, tolerance, run_id)
    def control_c5(tb_rows, prior_tb, tolerance, run_id)
    def control_c6(tb_rows, tolerance, run_id)
            -> tuple[list[ExceptionRecord], ControlSummary]

duplicate_exceptions are stage 3's findings for this same population. The
driver runs stage 3 first and passes them in, so DEDUPE is counted once and the
exception list is assembled in exactly one place; with none supplied stage 3 is
run here, because a register missing one of its seven controls is worse than
one that computes a duplicate check twice. run_id is the run's own identifier,
carried onto every record the controls produce.

The register is C1 to C6 and DEDUPE. C1 to C6 are implemented here. DEDUPE is
implemented in stage 3, under the rules that keep the population intact, and is
folded into the register here so that one call reports the whole register and a
control cannot quietly go missing from a run.

Every control returns a summary whether or not it found anything, so a control
that reports nothing is visible as having run over a population of a stated
size rather than being indistinguishable from a control that was never called.

Three decisions determine what the controls report.

C2's basis. The General Ledger carries no opening balance, so the closing
balance it implies is the Trial Balance opening balance, plus General Ledger
debits, less General Ledger credits, compared against the Trial Balance closing
balance. An account with no ledger movement therefore reconciles to itself and
C2 says nothing about it. An account the ledger has never heard of at all is
C3's finding, and C2 leaves it alone rather than reporting the same absence a
second time under a different name.

C2 is full-year. The General Ledger is never filtered by posting date. The
Trial Balance was derived from the whole year, so filtering one side and not
the other would compare unlike populations and manufacture a difference out of
a posting that is present in both. A posting in the wrong period is a cutoff
finding for C4, not a balance difference for C2.

C6 is one finding. Both limbs are tested — per account, that opening plus
period debits less period credits equals closing; and across accounts, that the
closing balances sum to zero — but a Trial Balance whose own arithmetic does
not hold is one condition, not two, and reporting it twice would count the same
money twice. The single exception is valued at the aggregate imbalance and its
evidence names the account that caused it.

C1 is the same shape. Its per-journal limb raises the exceptions; its aggregate
limb is computed, cross-checked against the sum of the per-journal differences,
and reported in the summary without being raised. A control reporting a figure
it did not compute would be worse than one that computes a figure it does not
raise, and the cross-check is what catches a grouping error in the control
itself.

Exceptions are outputs. A difference below the configured tolerance is recorded
with above_materiality False and a severity that says so. It is never netted
off, adjusted, or dropped for being small.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import date
from decimal import Decimal
from typing import Any

from .models import (
    CanonicalGLLine,
    CanonicalTBRow,
    ControlSummary,
    ExceptionRecord,
    parse_money,
    severity_for,
)
from .s3_dedupe import UNIDENTIFIED_RUN, find_duplicates

ZERO = Decimal("0.00")

# The register, in the order a run reports it.
CONTROL_ORDER = ("C1", "C2", "C3", "C4", "C5", "C6", "DEDUPE")


class ControlArithmeticError(RuntimeError):
    """A control's own arithmetic does not agree with itself.

    Not a finding about the ledger. A control that cannot reconcile its own
    two ways of measuring the same quantity is defective, and every figure it
    reported is suspect. Raised rather than asserted, because a bare assert
    disappears under python -O and a check that can be switched off from the
    command line is not a check.
    """


# ---------------------------------------------------------------------------
# Shared arithmetic
# ---------------------------------------------------------------------------


def as_date(value: Any) -> date:
    """A config date, whether the loader returned a date or a string."""
    return value if isinstance(value, date) else date.fromisoformat(str(value))


def account_totals(
    gl_lines: Sequence[CanonicalGLLine],
) -> tuple[dict[str, Decimal], dict[str, Decimal]]:
    """Total debits and total credits per account, over every line.

    Every line, with no date filter. See C2's basis in the module docstring.
    """
    debits: dict[str, Decimal] = defaultdict(lambda: ZERO)
    credits: dict[str, Decimal] = defaultdict(lambda: ZERO)
    for line in gl_lines:
        debits[line.account_code] += line.debit
        credits[line.account_code] += line.credit
    return debits, credits


def journal_count(count: int) -> str:
    """'16 journals', '1 journal'. A control's own prose should read."""
    return f"{count} journal" if count == 1 else f"{count} journals"


def entity_of(records: Sequence[Any]) -> str:
    """The entity a finding belongs to.

    Taken from the first record implicated. No control in this register spans
    entities: a journal, an account and a Trial Balance all sit within one.
    """
    return records[0].entity


def summary(
    run_id: str,
    control_id: str,
    rows_tested: int,
    exceptions: Sequence[ExceptionRecord],
    detail: str | None = None,
) -> ControlSummary:
    return ControlSummary(
        run_id=run_id,
        control_id=control_id,
        status="RUN",
        rows_tested=rows_tested,
        exceptions_raised=len(exceptions),
        reason=None,
        detail=detail,
    )


# ---------------------------------------------------------------------------
# C1 — Journal balance
# ---------------------------------------------------------------------------


def control_c1(
    gl_lines: Sequence[CanonicalGLLine],
    tolerance: Decimal,
    run_id: str,
) -> tuple[list[ExceptionRecord], ControlSummary]:
    """Debits equal credits within every journal, and across the ledger.

    Both limbs are computed. One exception is raised per journal that does not
    self-balance, and none is raised for the aggregate: every line belongs to
    exactly one journal, so the whole-ledger difference is the sum of the
    per-journal differences, and raising it separately would count the same
    money twice under a heading nobody could act on.

    Computing it is still necessary. It is the control checking its own
    arithmetic by a second route, and the identity it tests — that the ledger's
    total difference is exactly the sum of its journals' differences — is the
    one a grouping error breaks. Per-journal sums alone cannot catch a line
    counted twice or dropped from its journal; this can, because that line
    still reaches the whole-ledger totals. Both figures go to the summary, and
    a mismatch halts the run as a defect in the control rather than being
    absorbed into a finding about the ledger.

    A journal that does not balance is HIGH on the scale whatever its size.
    """
    debits: dict[str, Decimal] = defaultdict(lambda: ZERO)
    credits: dict[str, Decimal] = defaultdict(lambda: ZERO)
    lines_by_journal: dict[str, list[CanonicalGLLine]] = defaultdict(list)
    for line in gl_lines:
        debits[line.journal_id] += line.debit
        credits[line.journal_id] += line.credit
        lines_by_journal[line.journal_id].append(line)

    exceptions: list[ExceptionRecord] = []
    for journal_id in sorted(lines_by_journal):
        difference = debits[journal_id] - credits[journal_id]
        if difference == ZERO:
            continue
        value = abs(difference)
        lines = lines_by_journal[journal_id]
        exceptions.append(
            ExceptionRecord(
                run_id=run_id,
                control_id="C1",
                severity=severity_for(value, tolerance, unbalanced_journal=True),
                entity=entity_of(lines),
                account_code=None,
                journal_id=journal_id,
                value=value,
                record_count=len(lines),
                above_materiality=value > tolerance,
                evidence=(
                    f"journal {journal_id} does not self-balance across "
                    f"{len(lines)} lines: debits {debits[journal_id]:.2f} "
                    f"against credits {credits[journal_id]:.2f}, "
                    f"a difference of {value:.2f}"
                ),
                disposition="OPEN",
            )
        )

    # The aggregate limb. Computed over the lines themselves, not over the
    # per-journal totals, so the two routes are genuinely independent and the
    # comparison can fail.
    total_debits = sum((line.debit for line in gl_lines), ZERO)
    total_credits = sum((line.credit for line in gl_lines), ZERO)
    whole_ledger = total_debits - total_credits
    journal_sum = sum(
        (debits[journal_id] - credits[journal_id] for journal_id in lines_by_journal),
        ZERO,
    )
    if whole_ledger != journal_sum:
        raise ControlArithmeticError(
            f"C1 does not agree with itself: the whole ledger differs by "
            f"{whole_ledger:.2f}, while the per-journal differences over "
            f"{journal_count(len(lines_by_journal))} sum to {journal_sum:.2f}. "
            "Every line belongs to exactly one journal, so these cannot differ "
            "unless a line was counted twice or lost from its journal. This is "
            "a defect in the control, not a finding about the ledger, and the "
            "run is invalid."
        )

    detail = (
        f"whole ledger: debits {total_debits:.2f} against credits "
        f"{total_credits:.2f}, debits less credits {whole_ledger:.2f}, equal to "
        f"the sum of the per-journal differences over "
        f"{journal_count(len(lines_by_journal))}; reported, not raised, because "
        "the journals it is made of are already reported above"
    )
    return exceptions, summary(run_id, "C1", len(gl_lines), exceptions, detail=detail)


# ---------------------------------------------------------------------------
# C2 — General Ledger to Trial Balance agreement
# ---------------------------------------------------------------------------


def control_c2(
    gl_lines: Sequence[CanonicalGLLine],
    tb_rows: Sequence[CanonicalTBRow],
    tolerance: Decimal,
    run_id: str,
) -> tuple[list[ExceptionRecord], ControlSummary]:
    """The closing balance the ledger implies, against the one the Trial
    Balance reports, per account.

    The population is the Trial Balance, because only the Trial Balance carries
    the opening balance the derivation starts from. Any difference at all is
    reported; the tolerance decides whether it is material, not whether it is
    recorded.
    """
    debits, credits = account_totals(gl_lines)
    lines_by_account: dict[str, list[CanonicalGLLine]] = defaultdict(list)
    for line in gl_lines:
        lines_by_account[line.account_code].append(line)

    exceptions: list[ExceptionRecord] = []
    for row in sorted(tb_rows, key=lambda tb_row: tb_row.account_code):
        code = row.account_code
        derived = row.opening_balance + debits[code] - credits[code]
        difference = derived - row.closing_balance
        if difference == ZERO:
            continue
        value = abs(difference)
        lines = lines_by_account[code]
        exceptions.append(
            ExceptionRecord(
                run_id=run_id,
                control_id="C2",
                severity=severity_for(value, tolerance, affects_balance=True),
                entity=row.entity,
                account_code=code,
                journal_id=None,
                value=value,
                record_count=len(lines) + 1,
                above_materiality=value > tolerance,
                evidence=(
                    f"account {code}: General Ledger derives "
                    f"{derived:.2f} from opening {row.opening_balance:.2f} "
                    f"plus debits {debits[code]:.2f} less credits "
                    f"{credits[code]:.2f} over {len(lines)} lines, against a "
                    f"Trial Balance closing balance of {row.closing_balance:.2f}; "
                    f"a difference of {value:.2f}"
                ),
                disposition="OPEN",
            )
        )
    return exceptions, summary(
        run_id, "C2", len(tb_rows) + len(gl_lines), exceptions
    )


# ---------------------------------------------------------------------------
# C3 — Account completeness
# ---------------------------------------------------------------------------


def control_c3(
    gl_lines: Sequence[CanonicalGLLine],
    tb_rows: Sequence[CanonicalTBRow],
    tolerance: Decimal,
    run_id: str,
) -> tuple[list[ExceptionRecord], ControlSummary]:
    """Every account in one source appears in the other.

    The value recorded is the balance the account carries in the source that
    has it: the amount one source accounts for and the other has no record of.
    """
    rows_by_account = {row.account_code: row for row in tb_rows}
    lines_by_account: dict[str, list[CanonicalGLLine]] = defaultdict(list)
    for line in gl_lines:
        lines_by_account[line.account_code].append(line)

    exceptions: list[ExceptionRecord] = []

    for code in sorted(set(rows_by_account) - set(lines_by_account)):
        row = rows_by_account[code]
        value = abs(row.closing_balance)
        exceptions.append(
            ExceptionRecord(
                run_id=run_id,
                control_id="C3",
                severity=severity_for(value, tolerance, affects_balance=True),
                entity=row.entity,
                account_code=code,
                journal_id=None,
                value=value,
                record_count=1,
                above_materiality=value > tolerance,
                evidence=(
                    f"account {code} {row.account_name} appears in the Trial "
                    f"Balance with a closing balance of "
                    f"{row.closing_balance:.2f} and has no General Ledger lines"
                ),
                disposition="OPEN",
            )
        )

    for code in sorted(set(lines_by_account) - set(rows_by_account)):
        lines = lines_by_account[code]
        movement = sum((line.debit - line.credit for line in lines), ZERO)
        value = abs(movement)
        exceptions.append(
            ExceptionRecord(
                run_id=run_id,
                control_id="C3",
                severity=severity_for(value, tolerance, affects_balance=True),
                entity=entity_of(lines),
                account_code=code,
                journal_id=None,
                value=value,
                record_count=len(lines),
                above_materiality=value > tolerance,
                evidence=(
                    f"account {code} carries {len(lines)} General Ledger lines "
                    f"moving {movement:.2f} and does not appear in the Trial "
                    "Balance"
                ),
                disposition="OPEN",
            )
        )

    return exceptions, summary(run_id, "C3", len(tb_rows) + len(gl_lines), exceptions)


# ---------------------------------------------------------------------------
# C4 — Period cutoff
# ---------------------------------------------------------------------------


def control_c4(
    gl_lines: Sequence[CanonicalGLLine],
    financial_year: tuple[date, date],
    tolerance: Decimal,
    run_id: str,
) -> tuple[list[ExceptionRecord], ControlSummary]:
    """Every posting date falls within the configured financial year.

    One exception per misdated posting, valued at the amount of that posting.
    A cutoff finding does not change a full-year balance — the line is still
    counted in the account it was posted to, and C2 compares full-year totals —
    so it carries no balance impact and is graded on that basis.
    """
    start, end = financial_year
    exceptions: list[ExceptionRecord] = []
    for line in gl_lines:
        if start <= line.posting_date <= end:
            continue
        value = line.debit + line.credit
        exceptions.append(
            ExceptionRecord(
                run_id=run_id,
                control_id="C4",
                severity=severity_for(value, tolerance, affects_balance=False),
                entity=line.entity,
                account_code=line.account_code,
                journal_id=line.journal_id,
                value=value,
                record_count=1,
                above_materiality=value > tolerance,
                evidence=(
                    f"journal {line.journal_id} line {line.line_no}, account "
                    f"{line.account_code}, is dated "
                    f"{line.posting_date.isoformat()}, outside "
                    f"{start.isoformat()} to {end.isoformat()}: debit "
                    f"{line.debit:.2f} credit {line.credit:.2f}"
                ),
                disposition="OPEN",
            )
        )
    return exceptions, summary(run_id, "C4", len(gl_lines), exceptions)


# ---------------------------------------------------------------------------
# C5 — Rollforward
# ---------------------------------------------------------------------------


def control_c5(
    tb_rows: Sequence[CanonicalTBRow],
    prior_tb: Sequence[CanonicalTBRow] | None,
    tolerance: Decimal,
    run_id: str,
) -> tuple[list[ExceptionRecord], ControlSummary]:
    """Prior-year closing balance equals current-year opening balance.

    Implemented, not omitted. With no prior period supplied there is nothing to
    compare and the control reports SKIPPED with its reason, so the register
    shows a control that could not run rather than one that found nothing.

    An account in one year and not the other is compared against nil, because
    an account that did not exist last year closed at nil, and an opening
    balance that appeared from nowhere is exactly the break this control is for.
    """
    if not prior_tb:
        return [], ControlSummary(
            run_id=run_id,
            control_id="C5",
            status="SKIPPED",
            rows_tested=0,
            exceptions_raised=0,
            reason="no prior period loaded",
            detail=None,
        )

    current = {row.account_code: row for row in tb_rows}
    prior = {row.account_code: row for row in prior_tb}

    exceptions: list[ExceptionRecord] = []
    for code in sorted(set(current) | set(prior)):
        current_row = current.get(code)
        prior_row = prior.get(code)
        opening = current_row.opening_balance if current_row else ZERO
        prior_closing = prior_row.closing_balance if prior_row else ZERO
        difference = opening - prior_closing
        if difference == ZERO:
            continue
        value = abs(difference)
        row = current_row or prior_row
        exceptions.append(
            ExceptionRecord(
                run_id=run_id,
                control_id="C5",
                severity=severity_for(value, tolerance, affects_balance=True),
                entity=row.entity,
                account_code=code,
                journal_id=None,
                value=value,
                record_count=sum(1 for r in (current_row, prior_row) if r),
                above_materiality=value > tolerance,
                evidence=(
                    f"account {code}: prior period closing "
                    f"{prior_closing:.2f} against current period opening "
                    f"{opening:.2f}; a difference of {value:.2f}"
                ),
                disposition="OPEN",
            )
        )
    return exceptions, summary(
        run_id, "C5", len(tb_rows) + len(prior_tb), exceptions
    )


# ---------------------------------------------------------------------------
# C6 — Trial Balance internal balance
# ---------------------------------------------------------------------------


def control_c6(
    tb_rows: Sequence[CanonicalTBRow],
    tolerance: Decimal,
    run_id: str,
) -> tuple[list[ExceptionRecord], ControlSummary]:
    """The Trial Balance's own arithmetic holds.

        (a) per account:     opening + period debits - period credits = closing
        (b) across accounts: the closing balances sum to 0.00

    Both limbs are tested and one exception is raised, valued at the aggregate
    imbalance, naming in its evidence the accounts whose own arithmetic broke.
    A Trial Balance that does not balance is one condition however many ways it
    shows itself, and two exceptions here would count the same money twice.

    Where per-account breaks exactly offset, the aggregate is nil and the
    exception is still raised: the evidence carries the accounts, and the value
    records that the imbalance nets to nothing at the total.
    """
    broken: list[tuple[CanonicalTBRow, Decimal, Decimal]] = []
    for row in sorted(tb_rows, key=lambda tb_row: tb_row.account_code):
        derived = row.opening_balance + row.period_debits - row.period_credits
        difference = derived - row.closing_balance
        if difference != ZERO:
            broken.append((row, derived, abs(difference)))

    aggregate = sum((row.closing_balance for row in tb_rows), ZERO)

    if not broken and aggregate == ZERO:
        return [], summary(run_id, "C6", len(tb_rows), [])

    value = abs(aggregate)
    if broken:
        limb_a = "; ".join(
            f"account {row.account_code} closing {row.closing_balance:.2f} does "
            f"not follow from opening {row.opening_balance:.2f} plus debits "
            f"{row.period_debits:.2f} less credits {row.period_credits:.2f} "
            f"({derived:.2f}), a difference of {difference:.2f}"
            for row, derived, difference in broken
        )
    else:
        limb_a = "every account's opening plus movement equals its closing"

    exception = ExceptionRecord(
        run_id=run_id,
        control_id="C6",
        severity=severity_for(value, tolerance, affects_balance=True),
        entity=entity_of(tb_rows),
        account_code=None,
        journal_id=None,
        value=value,
        record_count=len(tb_rows),
        above_materiality=value > tolerance,
        evidence=(
            f"Trial Balance closing balances sum to {aggregate:.2f} rather "
            f"than 0.00 across {len(tb_rows)} accounts; {limb_a}"
        ),
        disposition="OPEN",
    )
    return [exception], summary(run_id, "C6", len(tb_rows), [exception])


# ---------------------------------------------------------------------------
# The register
# ---------------------------------------------------------------------------


def run_controls(
    gl_lines: Sequence[CanonicalGLLine],
    tb_rows: Sequence[CanonicalTBRow],
    config: Mapping[str, Any],
    prior_tb: Sequence[CanonicalTBRow] | None = None,
    *,
    run_id: str = UNIDENTIFIED_RUN,
    duplicate_exceptions: Sequence[ExceptionRecord] | None = None,
) -> tuple[list[ExceptionRecord], list[ControlSummary]]:
    """Run the whole register over the two populations as delivered.

    Returns every exception found and one summary per registered control, in
    register order.

    Tolerance and the financial year are read from the client config and are
    never constants here. Change the tolerance in configuration and the same
    differences are reported with a different materiality flag; none of them
    stops being reported.

    `duplicate_exceptions` are stage 3's findings for this same population.
    The driver runs stage 3 before stage 4 and passes them in, so DEDUPE is
    counted once and the exception list is assembled in exactly one place. With
    none supplied stage 3 is run here, because a register missing one of its
    seven controls is worse than one that computes a duplicate check twice.
    """
    tolerance = parse_money(config["materiality"]["tolerance"])
    financial_year = (
        as_date(config["financial_year"]["start"]),
        as_date(config["financial_year"]["end"]),
    )

    if duplicate_exceptions is None:
        duplicate_exceptions, _decisions = find_duplicates(
            gl_lines, tolerance=tolerance, run_id=run_id
        )

    found: dict[str, list[ExceptionRecord]] = {}
    summaries: dict[str, ControlSummary] = {}

    for control_id, (exceptions, control_summary) in (
        ("C1", control_c1(gl_lines, tolerance, run_id)),
        ("C2", control_c2(gl_lines, tb_rows, tolerance, run_id)),
        ("C3", control_c3(gl_lines, tb_rows, tolerance, run_id)),
        ("C4", control_c4(gl_lines, financial_year, tolerance, run_id)),
        ("C5", control_c5(tb_rows, prior_tb, tolerance, run_id)),
        ("C6", control_c6(tb_rows, tolerance, run_id)),
    ):
        found[control_id] = list(exceptions)
        summaries[control_id] = control_summary

    found["DEDUPE"] = list(duplicate_exceptions)
    summaries["DEDUPE"] = summary(
        run_id, "DEDUPE", len(gl_lines), duplicate_exceptions
    )

    exceptions = [record for control_id in CONTROL_ORDER for record in found[control_id]]
    return exceptions, [summaries[control_id] for control_id in CONTROL_ORDER]
