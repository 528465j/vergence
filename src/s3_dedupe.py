"""Stage 3 — Deduplication. Deterministic, human adjudication. Phase 1.

Exact duplicates: a SHA-256 over entity, account_code, posting_date,
journal_id, line_no, debit, credit and description.

Suspected duplicates: the same amount, date and account under different journal
identifiers.

    def find_duplicates(lines, *, tolerance, run_id=UNIDENTIFIED_RUN)
            -> tuple[list[ExceptionRecord], list[DuplicateDecision]]
    def exact_key(line) -> str
    def suspected_key(line) -> str

tolerance comes from the client config and is required, with no default.
DEDUPE is a control in the register and every control flags its findings
against the engagement's materiality; a duplicate reported as immaterial
because no tolerance reached this function would be wrong in the one direction
nobody would check. run_id is the run's own identifier, which the driver
passes so that every record from one run can be tied to it.

Nothing is removed, filtered, collapsed or reordered. This module reads the
population and returns two lists of findings about it; the population itself is
handed straight through to stage 4 exactly as delivered.

That is not caution, it is correctness, for two reasons.

Two identical rows can be entirely legitimate. A recurring daily bank fee posts
the same amount to the same account on consecutive days, and a batch that
charges it twice on one day looks exactly like a duplicate and may be exactly
right. Only a human can tell the two apart, so what this stage produces is a
reversible decision record for someone to adjudicate, never an applied
correction.

And removing a row would silently change what another control reports. The
duplicated line in this dataset is a one-sided debit: delete it and journal
J-1015 balances, C1 finds nothing, and the General Ledger agrees with the
Trial Balance on that account. Three controls would fall silent because of a
row this stage quietly took away, and the run would look cleaner than the
ledger is. Stage 4 therefore reads the source as delivered, and a duplicate is
reported by every control it touches.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from collections.abc import Sequence
from datetime import datetime, timezone
from decimal import Decimal

from .models import (
    CanonicalGLLine,
    DuplicateDecision,
    ExceptionRecord,
    severity_for,
)

# Recorded against a decision nobody has adjudicated yet. A reviewer's name
# replaces it when the disposition moves off OPEN.
DECIDED_BY = "PIPELINE"

# Stamped on records produced outside a registered run, in a test or an
# exploratory call. The pipeline driver passes the run's own identifier.
UNIDENTIFIED_RUN = "UNIDENTIFIED-RUN"

# Field values are joined with a separator that cannot occur in ledger text, so
# no combination of adjacent fields can be made to collide with another.
FIELD_SEPARATOR = "\x1f"


# ---------------------------------------------------------------------------
# Keys
# ---------------------------------------------------------------------------


def _digest(values: Sequence[str]) -> str:
    return hashlib.sha256(FIELD_SEPARATOR.join(values).encode("utf-8")).hexdigest()


def exact_key(line: CanonicalGLLine) -> str:
    """The exact-duplicate hash: all eight identifying fields of a line.

    Money is hashed as its two-place string rather than as a Decimal, so two
    amounts that are equal hash alike whatever produced them.
    """
    return _digest(
        [
            line.entity,
            line.account_code,
            line.posting_date.isoformat(),
            line.journal_id,
            str(line.line_no),
            f"{line.debit:.2f}",
            f"{line.credit:.2f}",
            line.description,
        ]
    )


def suspected_key(line: CanonicalGLLine) -> str:
    """The suspected-duplicate hash: amount, date and account.

    Everything a re-keyed copy of a posting keeps. The journal identifier is
    excluded on purpose: a group is suspected precisely when these fields agree
    and the journal identifiers do not.
    """
    return _digest(
        [
            line.entity,
            line.account_code,
            line.posting_date.isoformat(),
            f"{line.debit:.2f}",
            f"{line.credit:.2f}",
        ]
    )


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


def find_duplicates(
    lines: Sequence[CanonicalGLLine],
    *,
    tolerance: Decimal,
    run_id: str = UNIDENTIFIED_RUN,
) -> tuple[list[ExceptionRecord], list[DuplicateDecision]]:
    """Flag exact and suspected duplicates in the population as delivered.

    Returns one exception and one reversible decision per duplicate group, in
    the order the groups first appear in the population. `lines` is only ever
    read; the records are frozen, so this cannot alter the population even by
    accident.

    `tolerance` comes from the client config and has no default. DEDUPE is a
    control in the register and every control flags its findings against the
    engagement's materiality; a duplicate reported as immaterial because no
    tolerance reached this function would be wrong in the one direction nobody
    would check.

    A group can be both exact and suspected — two identical rows in one journal
    and a third carrying the same amount, date and account under another — and
    is then reported twice, once under each test, because the two say different
    things to the person adjudicating them.
    """
    exact_groups: dict[str, list[int]] = defaultdict(list)
    suspected_groups: dict[str, list[int]] = defaultdict(list)
    for index, line in enumerate(lines):
        exact_groups[exact_key(line)].append(index)
        suspected_groups[suspected_key(line)].append(index)

    exceptions: list[ExceptionRecord] = []
    decisions: list[DuplicateDecision] = []
    decided_at = datetime.now(timezone.utc)

    for digest, indices in _groups_in_file_order(exact_groups):
        if len(indices) < 2:
            continue
        exception, decision = _report(
            lines, indices, digest, "EXACT", tolerance, run_id, decided_at
        )
        exceptions.append(exception)
        decisions.append(decision)

    for digest, indices in _groups_in_file_order(suspected_groups):
        if len(indices) < 2:
            continue
        # Same amount, date and account is only suspicious across journals.
        # Within one journal it is an ordinary two-line posting, and where the
        # whole row repeats the exact test has already reported it.
        if len({lines[index].journal_id for index in indices}) < 2:
            continue
        exception, decision = _report(
            lines, indices, digest, "SUSPECTED", tolerance, run_id, decided_at
        )
        exceptions.append(exception)
        decisions.append(decision)

    return exceptions, decisions


def _groups_in_file_order(groups: dict[str, list[int]]) -> list[tuple[str, list[int]]]:
    """Groups ordered by where they first appear, so a run is reproducible."""
    return sorted(groups.items(), key=lambda item: item[1][0])


def _report(
    lines: Sequence[CanonicalGLLine],
    indices: Sequence[int],
    digest: str,
    kind: str,
    tolerance: Decimal,
    run_id: str,
    decided_at: datetime,
) -> tuple[ExceptionRecord, DuplicateDecision]:
    """Describe one duplicate group as an exception and a reversible decision.

    The value is the excess the duplication represents: every occurrence after
    the first. For a pair that is one line's worth, which is the amount that
    would come out of the ledger if a reviewer decided the copy was wrong. The
    decision itself decides nothing of the sort — it records that the group was
    found, by whom, and that it can be undone.
    """
    first = lines[indices[0]]
    amount = first.debit + first.credit
    value = (len(indices) - 1) * amount
    decision_id = f"DUP-{kind}-{digest[:12]}"

    if kind == "EXACT":
        journal_id: str | None = first.journal_id
        evidence = (
            f"{len(indices)} lines identical across all eight key fields: "
            f"population rows {list(indices)}, journal {first.journal_id} "
            f"line {first.line_no}, account {first.account_code}, "
            f"dated {first.posting_date.isoformat()}, debit {first.debit:.2f} "
            f"credit {first.credit:.2f}; hash {digest[:12]}"
        )
    else:
        journals = sorted({lines[index].journal_id for index in indices})
        # Left as None because the group spans journals; the journals are named
        # in the evidence rather than one of them being picked to stand for all.
        journal_id = None
        evidence = (
            f"{len(indices)} lines share account {first.account_code}, date "
            f"{first.posting_date.isoformat()}, debit {first.debit:.2f} and "
            f"credit {first.credit:.2f} under {len(journals)} journal "
            f"identifiers ({', '.join(journals)}): population rows "
            f"{list(indices)}; hash {digest[:12]}"
        )

    exception = ExceptionRecord(
        run_id=run_id,
        control_id="DEDUPE",
        # A duplicated posting moves a reported balance, so it is graded on the
        # scale like any other difference of its size.
        severity=severity_for(value, tolerance, affects_balance=True),
        entity=first.entity,
        account_code=first.account_code,
        journal_id=journal_id,
        value=value,
        record_count=len(indices),
        above_materiality=value > tolerance,
        evidence=evidence,
        disposition="OPEN",
    )

    decision = DuplicateDecision(
        decision_id=decision_id,
        kind=kind,
        row_indices=list(indices),
        decided_by=DECIDED_BY,
        decided_at=decided_at,
        # Flagged and left in the population. Nothing has been applied, which
        # is why every one of these is reversible.
        disposition="OPEN",
        reversible=True,
    )
    return exception, decision
