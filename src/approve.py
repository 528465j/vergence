"""The human approval gate. Deterministic.

    python -m src.approve --client CLIENT_A                  list, change nothing
    python -m src.approve --client CLIENT_A --approve-all
    python -m src.approve --client CLIENT_A --approve "Co Code"

    def main(argv=None) -> int
    def load_queue(path) -> dict
    def queued(queue, dataset) -> tuple[list[MappingProposal], list[MappingProposal]]
    def approve(queue, registry, *, columns, approved_at) -> tuple[dict, list[Written]]

Reads the review queue the run wrote to out/review_queue.json and, on
approval, records the complete settled mapping for that provider in
registry/mappings.json. A later run then resolves those columns at tier 1,
deterministically and without a model.

A separate command on purpose. The moment a person takes responsibility for a
mapping is a distinct act and appears in shell history as one; it is not a flag
on a pipeline run, where it could be set once in a script and never seen again.

Listing is the default and writes nothing. Approval has to be asked for, in
words, and names either every queued column or the specific column being
approved.

Two consequences worth stating, because neither is obvious from the command:

  * Approving anything makes the provider known to the registry, and the gate
    treats a known provider differently: a later proposal above the confidence
    gate resolves without being queued, because the first-mapping clause has
    been satisfied. Approving one column is a decision about the provider as
    well as about that column.
  * A queued column nothing proposed a field for cannot be approved. There is
    no mapping to record, and inventing one here would put a field in the
    registry that nothing ever suggested. Attach a model, or add the synonym.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .models import MappingProposal
from .s1_mapping import (
    MappingError,
    load_registry,
    merged_registry,
    proposal_model_for,
    registry_columns,
    registry_entry,
    save_registry,
)
from .s7_report import REVIEW_QUEUE_FILE

REPO_ROOT = Path(__file__).resolve().parent.parent
REGISTRY_PATH = REPO_ROOT / "registry" / "mappings.json"
QUEUE_PATH = REPO_ROOT / "out" / REVIEW_QUEUE_FILE


class ApprovalError(RuntimeError):
    """The approval cannot be carried out as asked."""


@dataclass(frozen=True)
class Written:
    """What was written for one dataset, and how it was arrived at."""

    dataset: str
    columns: int
    tiers: Mapping[int | None, int]
    approvers: Mapping[str, int]


# ---------------------------------------------------------------------------
# Reading the queue
# ---------------------------------------------------------------------------


def load_queue(path: Path = QUEUE_PATH) -> dict[str, Any]:
    """Read the review queue a run wrote."""
    if not Path(path).is_file():
        raise ApprovalError(
            f"no review queue at {path}. Run the pipeline first: "
            "python -m src.run --client <PROVIDER>"
        )
    queue = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(queue, dict) or "datasets" not in queue:
        raise ApprovalError(f"{path} is not a review queue")
    return queue


def _proposals(
    dataset: str, records: Sequence[Mapping[str, Any]]
) -> list[MappingProposal]:
    """Read proposals back through the dataset's own record.

    Validated on the way in rather than trusted as written. The queue is a file
    on disk between two commands, and a hand-edited canonical field is exactly
    the thing the enumeration exists to refuse.
    """
    model = proposal_model_for(dataset)
    try:
        return [model.model_validate(dict(record)) for record in records]
    except ValidationError as failure:
        raise ApprovalError(
            f"the review queue holds a {dataset} proposal that is not valid: {failure}"
        ) from None


def queued(
    queue: Mapping[str, Any], dataset: str
) -> tuple[list[MappingProposal], list[MappingProposal]]:
    """The settled and the awaiting proposals for one dataset."""
    block = queue["datasets"][dataset]
    return (
        _proposals(dataset, block.get("resolved") or ()),
        _proposals(dataset, block.get("review") or ()),
    )


def _ordered(
    queue: Mapping[str, Any], dataset: str, proposals: Sequence[MappingProposal]
) -> list[MappingProposal]:
    """Proposals in the order the columns appear in the file."""
    headers = list(queue["datasets"][dataset].get("headers") or ())
    position = {header: index for index, header in enumerate(headers)}
    return sorted(
        proposals, key=lambda p: position.get(p.source_column, len(position))
    )


# ---------------------------------------------------------------------------
# Approving
# ---------------------------------------------------------------------------


def approve(
    queue: Mapping[str, Any],
    registry: Mapping[str, Any],
    *,
    columns: Sequence[str] | None,
    approved_at: str,
) -> tuple[dict[str, Any], list[Written]]:
    """Record approvals and return the new registry and what it now holds.

    `columns` names the source columns being approved, or is None for all of
    them. What is written per dataset is the complete settled mapping — every
    column the resolver settled, plus the ones approved here — because a warm
    run resolves at tier 1 only for columns the registry actually carries.

    Nothing is written for a dataset with no approvals in it. A command that
    rewrote an untouched dataset would restamp an approval date nobody gave.
    """
    provider_id = queue["provider"]
    updated = dict(registry)
    written: list[Written] = []
    unknown = set(columns or ())

    for dataset in queue["datasets"]:
        resolved, review = queued(queue, dataset)
        if columns is None:
            approving = [p for p in review if p.canonical_field is not None]
        else:
            approving = [p for p in review if p.source_column in set(columns)]
            unknown -= {p.source_column for p in approving}
            unproposed = [p for p in approving if p.canonical_field is None]
            if unproposed:
                raise ApprovalError(
                    f"{dataset}: nothing proposed a field for "
                    f"{', '.join(repr(p.source_column) for p in unproposed)}, so "
                    "there is no mapping to approve. Attach a model, or add the "
                    "column to config/synonyms.yaml."
                )
        if not approving:
            continue

        settled = _ordered(queue, dataset, list(resolved) + approving)
        entry = registry_entry(
            settled,
            approved_at=approved_at,
            approved=[p.source_column for p in approving],
            previous=registry_columns(updated, provider_id, dataset),
        )
        updated = merged_registry(updated, provider_id, dataset, entry)
        written.append(
            Written(
                dataset=dataset,
                columns=len(entry["columns"]),
                tiers=Counter(p.source_tier for p in settled),
                approvers=Counter(
                    column["approved_by"] for column in entry["columns"].values()
                ),
            )
        )

    if unknown:
        raise ApprovalError(
            f"{', '.join(repr(column) for column in sorted(unknown))} "
            "is not awaiting a decision. Run the command with no approval flag "
            "to see what is."
        )
    if not written:
        raise ApprovalError(
            "nothing was approved. Every queued column either has no proposed "
            "field or was not named."
        )
    return updated, written


# ---------------------------------------------------------------------------
# Printing
# ---------------------------------------------------------------------------


def print_queue(queue: Mapping[str, Any]) -> None:
    """List what is awaiting a decision. Writes nothing."""
    provider = queue["provider"]
    waiting = queue.get("review_items", 0)
    print(f"REVIEW QUEUE  {provider}  |  run {queue['run_id']}  |  {waiting} awaiting")
    if queue.get("model"):
        print(f"  Proposed by {queue['model']} at a confidence gate of {queue['confidence_gate']}")
    else:
        print("  No model was attached to the run that wrote this queue.")

    for dataset in queue["datasets"]:
        resolved, review = queued(queue, dataset)
        print(
            f"  {dataset.upper():<4}{len(review)} awaiting, "
            f"{len(resolved)} already settled"
        )
        for proposal in review:
            field = proposal.canonical_field or "— nothing proposed"
            tier = "—" if proposal.source_tier is None else str(proposal.source_tier)
            print(
                f"      {proposal.source_column:<14}{field:<16}"
                f"{proposal.confidence:.2f}  tier {tier}"
            )
            print(f"        {proposal.rationale}")

    print("\n  Nothing was written. To approve:")
    print(f"      python -m src.approve --client {provider} --approve-all")
    print(f'      python -m src.approve --client {provider} --approve "<column>"')


def print_written(provider: str, written: Sequence[Written], path: Path) -> None:
    """Say what was recorded, for whom, and where."""
    total = sum(record.columns for record in written)
    print(f"APPROVED  {provider}")
    for record in written:
        tiers = " ".join(
            f"tier{tier}={record.tiers.get(tier, 0)}" for tier in (1, 2, 3)
        )
        approvers = " · ".join(
            f"{who} {count}" for who, count in sorted(record.approvers.items())
        )
        print(
            f"  {record.dataset.upper():<4}{record.columns:>3} columns written  "
            f"{tiers}   approved_by: {approvers}"
        )
    datasets = ", ".join(record.dataset for record in written)
    print(f"  Registry: {_relative(path)}  ({total} columns across {datasets})")
    print(f"  The next run of {provider} resolves these at tier 1, with no model call.")


def _relative(path: Path) -> str:
    try:
        return str(Path(path).relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


# ---------------------------------------------------------------------------
# Command
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m src.approve",
        description=(
            "Review and approve the column mappings a run could not settle on "
            "its own. Listing is the default and writes nothing."
        ),
    )
    parser.add_argument(
        "--client",
        required=True,
        help="provider identifier, matching the run that wrote the queue",
    )
    decision = parser.add_mutually_exclusive_group()
    decision.add_argument(
        "--approve-all",
        action="store_true",
        help="approve every queued column that carries a proposed field",
    )
    decision.add_argument(
        "--approve",
        action="append",
        metavar="COLUMN",
        help="approve one source column by name; repeatable",
    )
    args = parser.parse_args(argv)

    try:
        queue = load_queue(QUEUE_PATH)
        if queue.get("provider") != args.client:
            raise ApprovalError(
                f"the queue at {_relative(QUEUE_PATH)} was written for "
                f"{queue.get('provider')}, not {args.client}. Run the pipeline "
                f"for {args.client} first."
            )

        if not (args.approve_all or args.approve):
            print_queue(queue)
            return 0

        registry = load_registry(REGISTRY_PATH)
        updated, written = approve(
            queue,
            registry,
            columns=None if args.approve_all else list(args.approve),
            approved_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        )
        path = save_registry(updated, REGISTRY_PATH)
        print_written(args.client, written, path)
    except (ApprovalError, MappingError, json.JSONDecodeError) as failure:
        print(f"APPROVAL FAILED  {type(failure).__name__}: {failure}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
