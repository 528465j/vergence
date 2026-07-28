"""Stage 7 — Output, documentation and audit trail. Deterministic.

Four artefacts, written under out/:

  1. reconciliation_statement.md  value in scope, value agreed, and the
     exceptions by control and by value.
  2. exception_register.csv       one row per ExceptionRecord.
  3. run_log.json                 run identifier, timestamp, config and
     registry versions, every source file SHA-256, and every counter.
  4. review_queue.json            what stage 1 could not settle on its own.

The review queue is written on every run, including runs that need nothing
decided. A file that is only written when there is work would leave the last
run's queue on disk looking like this run's.

    def write_reports(outcome, out_dir) -> list[Path]
    def write_review_queue(path, ...) -> Path
    def print_run_summary(outcome) -> None

    class RunOutcome          everything one run produced, assembled by the driver
    class DatasetOutcome      one source file, and what became of its rows
    class DatasetResolution   how stage 1 resolved one file's columns
    class MappingFigures      that resolution counted by tier

Figures derived from an outcome, all of them read by the two writers above and
available to anything else that needs them:

    def counters(outcome) -> dict[str, int]
    def control_total_holds(outcome) -> bool
    def by_control(outcome) -> dict[str, list[ExceptionRecord]]
    def agreement(outcome) -> tuple[Decimal, Decimal, list[str]]

Both writers take the whole RunOutcome rather than a handful of loose arguments. A
reconciliation statement needs the Trial Balance to state what was in scope,
the run log needs the source hashes, and a reporting layer that recomputed
either from a subset would be free to disagree with the run it is describing.
Every figure here is read off one object, and this module computes no
arithmetic that a control has not already performed.

Exception values are reported by control and are never summed into one total.
The controls measure unlike quantities: C3's finding is a balance one source
carries and the other has no record of, C4's is the amount of a posting in the
wrong period, and neither is a difference between two figures the way C2's is.
A single total would add them into a number that measures nothing. One root
cause can also surface under several controls at once, so even the per-control
values do not add across the register.
"""

from __future__ import annotations

import csv
import json
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from .models import (
    CanonicalGLLine,
    CanonicalTBRow,
    ControlSummary,
    DuplicateDecision,
    ExceptionRecord,
    MappingProposal,
    SourceFile,
    parse_money,
)

ZERO = Decimal("0.00")

STATEMENT_FILE = "reconciliation_statement.md"
REGISTER_FILE = "exception_register.csv"
RUN_LOG_FILE = "run_log.json"
REVIEW_QUEUE_FILE = "review_queue.json"

REGISTER_COLUMNS = (
    "run_id",
    "control_id",
    "severity",
    "entity",
    "account_code",
    "journal_id",
    "value",
    "record_count",
    "above_materiality",
    "evidence",
    "disposition",
)

# The register order, so a control that found nothing still has a place.
CONTROL_ORDER = ("C1", "C2", "C3", "C4", "C5", "C6", "DEDUPE")


# ---------------------------------------------------------------------------
# What one run produced
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MappingFigures:
    """One file's columns, counted by the tier that settled them.

    The tiers count every column stage 1 formed a view about, resolved or
    queued, so they add to the columns the file carries. review counts what
    is waiting on a person, and overlaps the tier it was proposed at.
    """

    tier1: int
    tier2: int
    tier3: int
    review: int

    @property
    def columns(self) -> int:
        return self.tier1 + self.tier2 + self.tier3

    def line(self) -> str:
        return (
            f"tier1={self.tier1} tier2={self.tier2} "
            f"tier3={self.tier3} review={self.review}"
        )

    def as_dict(self) -> dict[str, int]:
        return {
            "tier1": self.tier1,
            "tier2": self.tier2,
            "tier3": self.tier3,
            "review": self.review,
        }


@dataclass(frozen=True)
class DatasetResolution:
    """How stage 1 resolved one file's columns, before a row was validated.

    Held whole rather than reduced to the mapping, because the review queue and
    the run log both describe how each column was arrived at, and a mapping on
    its own no longer says.

    missing_fields names the canonical fields nothing resolved to. It is the
    test of whether the mapping is usable: a file missing a field its record
    requires cannot produce a single valid row, and the run stops rather than
    quarantining every row of a file that was only ever half read.
    """

    dataset: str
    source: SourceFile
    mapping: Mapping[str, str]
    headers: Sequence[str]
    resolved: Sequence[MappingProposal]
    review: Sequence[MappingProposal]
    missing_fields: Sequence[str]

    @property
    def complete(self) -> bool:
        return not self.missing_fields

    @property
    def figures(self) -> MappingFigures:
        tiers = Counter(
            proposal.source_tier
            for proposal in list(self.resolved) + list(self.review)
        )
        return MappingFigures(
            tier1=tiers[1], tier2=tiers[2], tier3=tiers[3], review=len(self.review)
        )


@dataclass(frozen=True)
class DatasetOutcome:
    """One source file, and what became of its rows."""

    resolution: DatasetResolution
    rows_received: int
    rows_accepted: int
    quarantined: Sequence[Mapping[str, Any]]

    @property
    def dataset(self) -> str:
        return self.resolution.dataset

    @property
    def source(self) -> SourceFile:
        return self.resolution.source

    @property
    def mapping(self) -> Mapping[str, str]:
        return self.resolution.mapping

    @property
    def figures(self) -> MappingFigures:
        return self.resolution.figures

    @property
    def rows_quarantined(self) -> int:
        return len(self.quarantined)


@dataclass(frozen=True)
class RunOutcome:
    """Everything one run of the pipeline produced.

    Assembled by the driver, read by this module. Frozen: a report describes a
    run that has already happened and cannot alter it.
    """

    run_id: str
    started_at: datetime
    finished_at: datetime
    # Paths are recorded relative to this, so an artefact describes a
    # repository rather than one machine's directory layout.
    repo_root: Path
    config: Mapping[str, Any]
    config_path: Path
    config_sha256: str
    registry_path: Path
    registry_sha256: str
    registry_version: Any
    mapping_resolver: str
    # The class that was actually asked, and how many times it was asked. Both
    # are recorded rather than inferred from the configuration, so a stub can
    # never be reported as a live call and a model that was attached but never
    # needed reports the zero calls it made.
    model_name: str | None
    model_calls: int
    datasets: Sequence[DatasetOutcome]
    gl_lines: Sequence[CanonicalGLLine]
    tb_rows: Sequence[CanonicalTBRow]
    exceptions: Sequence[ExceptionRecord]
    summaries: Sequence[ControlSummary]
    decisions: Sequence[DuplicateDecision]

    @property
    def duration_seconds(self) -> float:
        return (self.finished_at - self.started_at).total_seconds()

    @property
    def sources(self) -> list[SourceFile]:
        return [dataset.source for dataset in self.datasets]


# ---------------------------------------------------------------------------
# Figures, all derived from the run
# ---------------------------------------------------------------------------


def counters(outcome: RunOutcome) -> dict[str, int]:
    """Every counter the run produced, in one place.

    Derived here rather than accumulated as the run goes, so the summary on the
    console and the counters in the log cannot drift apart: they are the same
    function of the same object.
    """
    rows_received = sum(dataset.rows_received for dataset in outcome.datasets)
    rows_accepted = sum(dataset.rows_accepted for dataset in outcome.datasets)
    rows_quarantined = sum(dataset.rows_quarantined for dataset in outcome.datasets)
    above = sum(1 for record in outcome.exceptions if record.above_materiality)
    return {
        "source_files": len(outcome.datasets),
        "columns_mapped": sum(len(dataset.mapping) for dataset in outcome.datasets),
        "columns_in_review": sum(
            dataset.figures.review for dataset in outcome.datasets
        ),
        "model_calls": outcome.model_calls,
        "rows_received": rows_received,
        "rows_accepted": rows_accepted,
        "rows_quarantined": rows_quarantined,
        "gl_lines": len(outcome.gl_lines),
        "tb_rows": len(outcome.tb_rows),
        "controls_registered": len(outcome.summaries),
        "controls_run": sum(1 for s in outcome.summaries if s.status == "RUN"),
        "controls_skipped": sum(1 for s in outcome.summaries if s.status == "SKIPPED"),
        "duplicate_decisions": len(outcome.decisions),
        "exceptions": len(outcome.exceptions),
        "exceptions_above_materiality": above,
        "exceptions_below_materiality": len(outcome.exceptions) - above,
    }


def control_total_holds(outcome: RunOutcome) -> bool:
    """rows_received == rows_accepted + rows_quarantined, over the whole run."""
    figures = counters(outcome)
    return figures["rows_received"] == (
        figures["rows_accepted"] + figures["rows_quarantined"]
    )


def by_control(outcome: RunOutcome) -> dict[str, list[ExceptionRecord]]:
    """Exceptions grouped by control, in register order, every control present."""
    grouped: dict[str, list[ExceptionRecord]] = {
        control_id: [] for control_id in CONTROL_ORDER
    }
    for record in outcome.exceptions:
        grouped.setdefault(record.control_id, []).append(record)
    return grouped


def agreement(outcome: RunOutcome) -> tuple[Decimal, Decimal, list[str]]:
    """Value in scope, value agreed, and the accounts carrying an exception.

    The population in scope is the Trial Balance, taken at the absolute value of
    each closing balance: it is the only source carrying a balance per account
    to place in scope. An account agrees when nothing at account level was
    raised against it — C2, which tests it against the ledger, and C3, which
    tests that both sources have heard of it. An account the Trial Balance does
    not carry at all has no Trial Balance value to put in scope, and C3 reports
    it rather than this line quietly absorbing it.
    """
    flagged = sorted(
        {
            record.account_code
            for record in outcome.exceptions
            if record.control_id in ("C2", "C3") and record.account_code
        }
    )
    in_scope = sum((abs(row.closing_balance) for row in outcome.tb_rows), ZERO)
    agreed = sum(
        (
            abs(row.closing_balance)
            for row in outcome.tb_rows
            if row.account_code not in flagged
        ),
        ZERO,
    )
    return in_scope, agreed, flagged


# ---------------------------------------------------------------------------
# Artefacts
# ---------------------------------------------------------------------------


def write_reports(outcome: RunOutcome, out_dir: Path) -> list[Path]:
    """Write the three reporting artefacts and return their paths."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    return [
        _write_statement(outcome, out_dir / STATEMENT_FILE),
        _write_register(outcome, out_dir / REGISTER_FILE),
        _write_run_log(outcome, out_dir / RUN_LOG_FILE),
    ]


def _proposal_record(proposal: MappingProposal) -> dict[str, Any]:
    """One proposal as it is written to the queue, and read back by the gate."""
    return {
        "source_column": proposal.source_column,
        "canonical_field": proposal.canonical_field,
        "confidence": proposal.confidence,
        "source_tier": proposal.source_tier,
        "rationale": proposal.rationale,
    }


def write_review_queue(
    out_dir: Path,
    *,
    run_id: str,
    provider: str,
    registry_version: Any,
    gate: float,
    model_name: str | None,
    resolutions: Sequence[DatasetResolution],
) -> Path:
    """Write what stage 1 could not settle, and what it settled alongside it.

    Both halves are recorded. The queue on its own would let a reviewer approve
    five columns and leave the other seven with nowhere to be written down,
    and the approval gate needs the complete settled mapping to record a
    provider a later run can resolve entirely at tier 1.

    Written on every run. An empty queue is a statement that nothing is
    waiting, which is not the same as no statement at all.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / REVIEW_QUEUE_FILE
    payload = {
        "run_id": run_id,
        "provider": provider,
        "registry_version": registry_version,
        "confidence_gate": gate,
        "model": model_name,
        "review_items": sum(len(r.review) for r in resolutions),
        "datasets": {
            resolution.dataset: {
                "source": {
                    "filename": resolution.source.filename,
                    "sha256": resolution.source.sha256,
                },
                "headers": list(resolution.headers),
                "complete": resolution.complete,
                "missing_fields": list(resolution.missing_fields),
                "figures": resolution.figures.as_dict(),
                "resolved": [
                    _proposal_record(proposal) for proposal in resolution.resolved
                ],
                "review": [
                    _proposal_record(proposal) for proposal in resolution.review
                ],
            }
            for resolution in resolutions
        },
    }
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
        handle.write("\n")
    return path


def _write_statement(outcome: RunOutcome, path: Path) -> Path:
    config = outcome.config
    # Read by the same parser as every other monetary value in the pipeline.
    tolerance = parse_money(config["materiality"]["tolerance"])
    currency = config["materiality"]["currency"]
    in_scope, agreed, flagged = agreement(outcome)
    not_agreed = in_scope - agreed
    grouped = by_control(outcome)
    summaries = {summary.control_id: summary for summary in outcome.summaries}
    accounts = len(outcome.tb_rows)

    lines = [
        "# Reconciliation statement",
        "",
        f"Run `{outcome.run_id}`",
        "",
        "| | |",
        "|---|---|",
        f"| Provider | {config['client_id']} |",
        f"| Engagement | {config['engagement_id']} |",
        f"| Period | {config['period']} |",
        f"| Materiality tolerance | {tolerance:,.2f} {currency} |",
        f"| General Ledger lines | {len(outcome.gl_lines)} |",
        f"| Trial Balance accounts | {accounts} |",
        "",
        "## Value in scope",
        "",
        "The population in scope is the Trial Balance, at the absolute value of",
        "each closing balance. An account is agreed when nothing was raised",
        "against it at account level: C2 tests its balance against the General",
        "Ledger, and C3 tests that both sources have heard of it at all.",
        "",
        "| | value | accounts |",
        "|---|---:|---:|",
        f"| Value in scope | {in_scope:,.2f} | {accounts} |",
        f"| Value agreed | {agreed:,.2f} | {accounts - len(flagged)} |",
        f"| Value on accounts carrying an exception | {not_agreed:,.2f} | {len(flagged)} |",
        "",
    ]
    if flagged:
        lines += [f"Accounts carrying an exception: {', '.join(flagged)}.", ""]

    lines += [
        "## Exceptions by control",
        "",
        "| control | exceptions | value | above materiality | below |",
        "|---|---:|---:|---:|---:|",
    ]
    for control_id in CONTROL_ORDER:
        records = grouped.get(control_id, [])
        summary = summaries.get(control_id)
        if summary is not None and summary.status == "SKIPPED":
            lines.append(f"| {control_id} | SKIPPED | — | — | — |")
            continue
        value = sum((record.value for record in records), ZERO)
        above = sum(1 for record in records if record.above_materiality)
        lines.append(
            f"| {control_id} | {len(records)} | {value:,.2f} | {above} | "
            f"{len(records) - above} |"
        )

    lines += [
        "",
        "These values are stated by control and are not summed. The controls",
        "measure unlike quantities: C3's value is a balance one source carries",
        "and the other has no record of, C4's is the amount of a posting in the",
        "wrong period, and neither is a difference between two figures the way",
        "C2's is. Adding them would produce a number that measures nothing.",
        "",
        "Nor do they add across the register, because one root cause can surface",
        "under several controls at once. Every difference is recorded whatever",
        "its size; the tolerance decides whether it is material, never whether",
        "it is reported.",
        "",
        "## Controls",
        "",
        "| control | status | rows tested | exceptions |",
        "|---|---|---:|---:|",
    ]
    for summary in outcome.summaries:
        status = summary.status
        if summary.reason:
            status = f"{status} — {summary.reason}"
        lines.append(
            f"| {summary.control_id} | {status} | {summary.rows_tested} | "
            f"{summary.exceptions_raised} |"
        )

    measured = [summary for summary in outcome.summaries if summary.detail]
    if measured:
        lines += [
            "",
            "What the controls measured beyond their findings. These figures are",
            "computed and reported; none of them is raised as an exception.",
            "",
        ]
        lines += [
            f"- **{summary.control_id}** — {summary.detail}" for summary in measured
        ]

    if outcome.decisions:
        lines += [
            "",
            "## Duplicate decisions",
            "",
            "Flagged and left in the population. Nothing was removed, so every",
            "control read the ledger exactly as delivered.",
            "",
            "| decision | kind | rows | disposition | reversible |",
            "|---|---|---|---|---|",
        ]
        for decision in outcome.decisions:
            lines.append(
                f"| {decision.decision_id} | {decision.kind} | "
                f"{', '.join(str(index) for index in decision.row_indices)} | "
                f"{decision.disposition} | {'yes' if decision.reversible else 'no'} |"
            )

    lines += [
        "",
        "## Exception register",
        "",
        "| control | severity | account | journal | value | material | evidence |",
        "|---|---|---|---|---:|---|---|",
    ]
    for record in outcome.exceptions:
        lines.append(
            f"| {record.control_id} | {record.severity} | "
            f"{record.account_code or '—'} | {record.journal_id or '—'} | "
            f"{record.value:,.2f} | "
            f"{'yes' if record.above_materiality else 'no'} | {record.evidence} |"
        )

    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _write_register(outcome: RunOutcome, path: Path) -> Path:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(REGISTER_COLUMNS)
        for record in outcome.exceptions:
            writer.writerow(
                [
                    record.run_id,
                    record.control_id,
                    record.severity,
                    record.entity,
                    record.account_code or "",
                    record.journal_id or "",
                    f"{record.value:.2f}",
                    record.record_count,
                    "true" if record.above_materiality else "false",
                    record.evidence,
                    record.disposition,
                ]
            )
    return path


def _write_run_log(outcome: RunOutcome, path: Path) -> Path:
    payload = {
        "run_id": outcome.run_id,
        "started_at": _stamp(outcome.started_at),
        "finished_at": _stamp(outcome.finished_at),
        "duration_seconds": round(outcome.duration_seconds, 3),
        "provider": outcome.config["client_id"],
        "engagement": outcome.config["engagement_id"],
        "period": outcome.config["period"],
        "config": {
            "path": _relative(outcome.config_path, outcome.repo_root),
            "sha256": outcome.config_sha256,
            # The config declares no version of its own; its hash is what
            # identifies the configuration this run was made under.
            "declared_version": outcome.config.get("config_version"),
        },
        "registry": {
            "path": _relative(outcome.registry_path, outcome.repo_root),
            "sha256": outcome.registry_sha256,
            "declared_version": outcome.registry_version,
        },
        "mapping": {
            "resolver": outcome.mapping_resolver,
            # The registry this run resolved against, restated here so the
            # mapping section says what it read as well as what it did. The
            # registry section above carries the same file's path and hash.
            "registry_version": outcome.registry_version,
            "model": outcome.model_name,
            "model_calls": outcome.model_calls,
            "by_dataset": {
                dataset.dataset: dataset.figures.as_dict()
                for dataset in outcome.datasets
            },
        },
        "sources": [
            {
                "dataset": dataset.dataset,
                "path": _relative(dataset.source.path, outcome.repo_root),
                "filename": dataset.source.filename,
                "sha256": dataset.source.sha256,
                "byte_size": dataset.source.byte_size,
                "encoding": dataset.source.encoding,
                "delimiter": dataset.source.delimiter,
                "received_at": _stamp(dataset.source.received_at),
                "columns_mapped": dict(dataset.mapping),
                "rows_received": dataset.rows_received,
                "rows_accepted": dataset.rows_accepted,
                "rows_quarantined": dataset.rows_quarantined,
            }
            for dataset in outcome.datasets
        ],
        "counters": counters(outcome),
        "control_total_holds": control_total_holds(outcome),
        "controls": [
            {
                "control_id": summary.control_id,
                "status": summary.status,
                "rows_tested": summary.rows_tested,
                "exceptions_raised": summary.exceptions_raised,
                "reason": summary.reason,
                "detail": summary.detail,
            }
            for summary in outcome.summaries
        ],
        "exception_value_by_control": {
            control_id: f"{sum((r.value for r in records), ZERO):.2f}"
            for control_id, records in by_control(outcome).items()
            if records
        },
        "duplicate_decisions": [
            {
                "decision_id": decision.decision_id,
                "kind": decision.kind,
                "row_indices": list(decision.row_indices),
                "decided_by": decision.decided_by,
                "decided_at": _stamp(decision.decided_at),
                "disposition": decision.disposition,
                "reversible": decision.reversible,
            }
            for decision in outcome.decisions
        ],
        "quarantine": [
            dict(entry) for dataset in outcome.datasets for entry in dataset.quarantined
        ],
    }
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
        handle.write("\n")
    return path


def _stamp(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


def _relative(path: Path, root: Path) -> str:
    """A path as the repository sees it, or unchanged if it sits outside."""
    try:
        return str(Path(path).relative_to(root))
    except ValueError:
        return str(path)


# ---------------------------------------------------------------------------
# Console summary
# ---------------------------------------------------------------------------


def mapping_report(
    resolutions: Sequence[Any],
    *,
    model_name: str | None,
    model_calls: int,
) -> list[str]:
    """The mapping block: one line per file, then the model that was asked.

    Rendered here for both the run summary and the review notice, so a run
    that stopped for a decision reports its mapping in exactly the figures a
    run that finished would.

    Naming the class is deliberate. The line states what actually ran, so a
    stub can never be presented as a live call, and a run with a model
    attached that needed nothing proposed reports the zero calls it made.
    """
    lines = [
        f"{resolution.dataset.upper():<4}{resolution.figures.line()}"
        for resolution in resolutions
    ]
    called = model_name or "no model attached"
    lines.append(f"{'':<36}| model calls: {model_calls} ({called})")
    return lines


def print_run_summary(outcome: RunOutcome) -> None:
    """Print the run summary. Every figure is read off the outcome."""
    figures = counters(outcome)
    config = outcome.config
    grouped = by_control(outcome)

    skipped = [
        f"{summary.control_id} SKIPPED - {summary.reason}"
        for summary in outcome.summaries
        if summary.status == "SKIPPED"
    ]
    controls = str(figures["controls_registered"])
    if skipped:
        controls = f"{controls}  ({'; '.join(skipped)})"

    # Controls that found nothing are named in the artefacts and in the
    # Controls run count; this line reports findings, so it carries only the
    # controls that raised one.
    found = " · ".join(
        f"{control_id} {len(records)}"
        for control_id, records in grouped.items()
        if records
    )

    total = "[control total OK]" if control_total_holds(outcome) else "[CONTROL TOTAL FAILED]"

    print(
        f"RUN {outcome.run_id}  |  provider={config['client_id']}  "
        f"engagement={config['engagement_id']}  period={config['period']}"
    )
    for label, text in (
        ("Sources", f"{figures['source_files']} files, SHA-256 recorded"),
        (
            "Rows",
            f"received {figures['rows_received']} | "
            f"accepted {figures['rows_accepted']} | "
            f"quarantined {figures['rows_quarantined']}   {total}",
        ),
        (
            "Mapping",
            "\n".join(
                mapping_report(
                    outcome.datasets,
                    model_name=outcome.model_name,
                    model_calls=outcome.model_calls,
                )
            ),
        ),
        ("Controls run", controls),
        (
            "Exceptions",
            f"{figures['exceptions']}  |  above materiality "
            f"{figures['exceptions_above_materiality']} | below "
            f"{figures['exceptions_below_materiality']}",
        ),
        ("By control", found or "none"),
        ("Duration", f"{outcome.duration_seconds:.3f} s"),
    ):
        first, *rest = str(text).split("\n")
        print(f"  {label:<15}: {first}")
        for line in rest:
            print(f"  {'':<15}  {line}")
