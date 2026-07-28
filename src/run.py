"""Pipeline driver.

    python -m src.run --client CLIENT_A
    python -m src.run --client CLIENT_A --no-llm

    def main(argv=None) -> int
    def reconcile(client_id, out_dir, *, llm) -> RunOutcome
    def resolve_dataset(dataset, path, config, registry, synonyms, llm)
            -> DatasetResolution
    def load_dataset(resolution, config, model) -> tuple[DatasetOutcome, list]
    def new_run_id(started_at) -> str
    def sha256_of(path) -> str

Sequence: register sources, resolve columns through the three tiers, validate,
find duplicates, run controls, write reports, print the run summary.

One codebase, one configuration file per provider. The provider identifier
selects its config and its two source files; nothing about a provider is
written here, and nothing about one is written in code anywhere else. What a
provider's columns mean is recorded in the registry, by a person, through
`python -m src.approve`.

Three exit codes, because a run has three outcomes and they are not the same:

    0  a completed reconciliation, whatever it found
    1  the pipeline could not run: a source that cannot be read, a mapping
       that cannot be resolved, or a control total that does not hold
    2  work is waiting on a person, and the run stopped before the controls

A run that finds exceptions exits 0. A reconciliation difference is an output,
not a failure, and a driver that returned non-zero for finding one would be
reporting the presence of evidence as an error. Exit 2 is not a failure either
— it is the human gate doing its job — which is why it is not folded in with 1.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import yaml

from .models import CanonicalGLLine, CanonicalTBRow, parse_money
from .s0_ingest import FormatDetectionError, read_rows, register_source
from .s1_mapping import (
    DATASET_FIELDS,
    MappingError,
    MappingRequest,
    StubMappingLLM,
    load_registry,
    load_synonyms,
    resolve_columns,
)
from .s2_validate import ControlTotalError, assert_control_total, validate_rows
from .s3_dedupe import find_duplicates
from .s4_reconcile import ControlArithmeticError, run_controls
from .s7_report import (
    DatasetOutcome,
    DatasetResolution,
    RunOutcome,
    mapping_report,
    print_run_summary,
    write_reports,
    write_review_queue,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = REPO_ROOT / "config"
DATA_DIR = REPO_ROOT / "data"
REGISTRY_PATH = REPO_ROOT / "registry" / "mappings.json"
SYNONYMS_PATH = REPO_ROOT / "config" / "synonyms.yaml"
OUT_DIR = REPO_ROOT / "out"

MAPPING_RESOLVER = "three-tier: registry, synonym, model"

# The canonical record each dataset resolves to.
DATASET_MODELS: dict[str, type] = {"gl": CanonicalGLLine, "tb": CanonicalTBRow}

REVIEW_EXIT = 2


class ReviewRequired(RuntimeError):
    """Columns need a decision before the run can go any further.

    Carries the resolutions rather than a message, so the driver can say which
    file, which columns and which canonical fields are missing, and name the
    command that resolves it.
    """

    def __init__(
        self,
        provider_id: str,
        resolutions: Sequence[DatasetResolution],
        queue_path: Path,
        model_name: str | None,
        model_calls: int,
    ) -> None:
        super().__init__(f"{provider_id}: columns awaiting a decision")
        self.provider_id = provider_id
        self.resolutions = resolutions
        self.queue_path = queue_path
        self.model_name = model_name
        self.model_calls = model_calls


class CountedModel:
    """A proposer, and a count of the times it was actually asked.

    Wrapped around whatever the caller supplied so the run reports calls that
    happened rather than calls the configuration implies. A model attached to a
    run that needs nothing proposed reports zero, which is the whole point of
    warming the registry.
    """

    def __init__(self, llm: Any = None) -> None:
        self._llm = llm
        self.calls = 0

    @property
    def attached(self) -> bool:
        return self._llm is not None

    @property
    def name(self) -> str | None:
        return None if self._llm is None else type(self._llm).__name__

    def propose(self, request: MappingRequest) -> list[Any]:
        self.calls += 1
        return self._llm.propose(request)


def new_run_id(started_at: datetime) -> str:
    """An identifier for one run: when it started, and which run it was.

    Stable for the whole run and written into all the artefacts, so a
    statement, a register, a queue and a log can be tied to each other and to
    nothing else.
    """
    stamp = started_at.strftime("%Y%m%dT%H%M%SZ")
    return f"RUN-{stamp}-{uuid4().hex[:6]}"


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def resolve_dataset(
    dataset: str,
    path: Path,
    config: Mapping[str, Any],
    registry: Mapping[str, Any],
    synonyms: Mapping[str, Any],
    llm: CountedModel,
) -> tuple[DatasetResolution, list[Mapping[str, Any]]]:
    """Stages 0 and 1 for one file: register it, then resolve its columns.

    Returns the resolution and the rows as they arrived, read once and carried
    rather than read again downstream: the hash was taken on arrival and the
    file is read-only thereafter, so a second read could only ever produce the
    same rows or a discrepancy nothing would notice.

    Separated from the loading below because the mapping decides whether there
    is anything worth loading. A file whose columns are half resolved would
    quarantine every row it has, and report a validation problem for what is
    really an unanswered question about the header.
    """
    source = register_source(path, config)
    headers, raw_rows = read_rows(source)
    mapping, resolved, review = resolve_columns(
        headers,
        raw_rows,
        config["client_id"],
        dataset,
        registry,
        synonyms=synonyms,
        gate=config["mapping"]["confidence_gate"],
        llm=llm if llm.attached else None,
    )
    settled = set(mapping.values())
    resolution = DatasetResolution(
        dataset=dataset,
        source=source,
        mapping=mapping,
        headers=headers,
        resolved=resolved,
        review=review,
        missing_fields=tuple(
            field for field in DATASET_FIELDS[dataset] if field not in settled
        ),
    )
    return resolution, raw_rows


def load_dataset(
    resolution: DatasetResolution,
    raw_rows: Sequence[Mapping[str, Any]],
    config: Mapping[str, Any],
    model: type,
) -> tuple[DatasetOutcome, list[Any]]:
    """Stage 2 for one file: read its rows under the resolved mapping.

    Returns the record of what happened to the file and the accepted records.
    """
    # Only the columns the mapping settled, renamed to canonical fields. Values
    # are untouched; parsing them is stage 2's decision to make. A column the
    # mapping did not settle is not dropped quietly — it is in the review queue
    # this run wrote, named, with the reason nothing resolved it.
    mapped_rows = [
        {
            canonical: raw_row[column]
            for column, canonical in resolution.mapping.items()
            if column in raw_row
        }
        for raw_row in raw_rows
    ]

    accepted, quarantined = validate_rows(
        mapped_rows,
        model,
        resolution.source,
        date_formats=config["parsing"]["date_formats"],
    )
    return (
        DatasetOutcome(
            resolution=resolution,
            rows_received=len(raw_rows),
            rows_accepted=len(accepted),
            quarantined=quarantined,
        ),
        accepted,
    )


def reconcile(client_id: str, out_dir: Path, *, llm: Any = None) -> RunOutcome:
    """Run the whole pipeline for one provider.

    Raises ReviewRequired when stage 1 could not settle a mapping on its own.
    Nothing downstream is reached in that case: an incomplete mapping must not
    be allowed to produce a population the controls would then measure.
    """
    started_at = datetime.now(timezone.utc)
    run_id = new_run_id(started_at)
    counted = llm if isinstance(llm, CountedModel) else CountedModel(llm)

    config_path = CONFIG_DIR / f"{client_id.lower()}.yaml"
    if not config_path.is_file():
        raise FileNotFoundError(f"no configuration for {client_id} at {config_path}")
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))

    registry = load_registry(REGISTRY_PATH)
    synonyms = load_synonyms(SYNONYMS_PATH)

    # -- stages 0 and 1 -----------------------------------------------------
    # Both files are resolved before either is loaded, so a run that stops for
    # a decision puts every column it could not settle in front of a person at
    # once rather than one file at a time.
    arrived = [
        resolve_dataset(
            dataset,
            DATA_DIR / f"{client_id.lower()}_{dataset}.csv",
            config,
            registry,
            synonyms,
            counted,
        )
        for dataset in ("gl", "tb")
    ]
    resolutions = [resolution for resolution, _ in arrived]

    queue_path = write_review_queue(
        out_dir,
        run_id=run_id,
        provider=config["client_id"],
        registry_version=registry.get("registry_version"),
        gate=config["mapping"]["confidence_gate"],
        model_name=counted.name,
        resolutions=resolutions,
    )

    if any(not resolution.complete for resolution in resolutions):
        raise ReviewRequired(
            config["client_id"],
            resolutions,
            queue_path,
            counted.name,
            counted.calls,
        )

    # -- stage 2 ------------------------------------------------------------
    loaded = [
        load_dataset(
            resolution, raw_rows, config, DATASET_MODELS[resolution.dataset]
        )
        for resolution, raw_rows in arrived
    ]
    (gl_dataset, gl_lines), (tb_dataset, tb_rows) = loaded
    datasets = (gl_dataset, tb_dataset)

    # Asserted per file inside stage 2 and again across the whole run. If the
    # rows that arrived are not the rows that were accounted for, no figure
    # computed after this point can be trusted and the run halts.
    assert_control_total(
        sum(dataset.rows_received for dataset in datasets),
        sum(dataset.rows_accepted for dataset in datasets),
        sum(dataset.rows_quarantined for dataset in datasets),
    )

    # -- stage 3 ------------------------------------------------------------
    tolerance = parse_money(config["materiality"]["tolerance"])
    duplicate_exceptions, decisions = find_duplicates(
        gl_lines, tolerance=tolerance, run_id=run_id
    )

    # -- stage 4 ------------------------------------------------------------
    # The population handed to the controls is the one stage 3 was given, row
    # for row. Stage 3 records decisions; it never applies them.
    exceptions, summaries = run_controls(
        gl_lines,
        tb_rows,
        config,
        prior_tb=None,
        run_id=run_id,
        duplicate_exceptions=duplicate_exceptions,
    )

    outcome = RunOutcome(
        run_id=run_id,
        started_at=started_at,
        finished_at=datetime.now(timezone.utc),
        repo_root=REPO_ROOT,
        config=config,
        config_path=config_path,
        config_sha256=sha256_of(config_path),
        registry_path=REGISTRY_PATH,
        registry_sha256=sha256_of(REGISTRY_PATH),
        registry_version=registry.get("registry_version"),
        mapping_resolver=MAPPING_RESOLVER,
        model_name=counted.name,
        model_calls=counted.calls,
        datasets=datasets,
        gl_lines=gl_lines,
        tb_rows=tb_rows,
        exceptions=exceptions,
        summaries=summaries,
        decisions=decisions,
    )

    # -- stage 7 ------------------------------------------------------------
    written = write_reports(outcome, out_dir) + [queue_path]
    print_run_summary(outcome)
    print(
        f"  {'Artefacts':<15}: "
        + ", ".join(_relative(path) for path in written)
    )
    return outcome


def _relative(path: Path) -> str:
    try:
        return str(Path(path).relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def report_review(failure: ReviewRequired) -> None:
    """Say what was mapped, what needs deciding, and the command that decides it."""
    waiting = sum(len(resolution.review) for resolution in failure.resolutions)
    print(
        f"REVIEW REQUIRED  {waiting} column(s) need a decision before "
        f"{failure.provider_id} can be reconciled."
    )
    lines = mapping_report(
        failure.resolutions,
        model_name=failure.model_name,
        model_calls=failure.model_calls,
    )
    print(f"  {'Mapping':<15}: {lines[0]}")
    for line in lines[1:]:
        print(f"  {'':<15}  {line}")
    for resolution in failure.resolutions:
        if not resolution.review:
            continue
        columns = ", ".join(
            proposal.source_column for proposal in resolution.review
        )
        print(f"  {resolution.dataset.upper():<4}{len(resolution.review)} awaiting: {columns}")
        for proposal in resolution.review:
            proposed = (
                "nothing proposed"
                if proposal.canonical_field is None
                else f"-> {proposal.canonical_field} ({proposal.confidence:.2f}, "
                f"tier {proposal.source_tier})"
            )
            print(f"      {proposal.source_column:<14}{proposed}")
        if resolution.missing_fields:
            print(
                f"      unresolved canonical fields: "
                f"{', '.join(resolution.missing_fields)}"
            )
    print(f"  Queue written to {_relative(failure.queue_path)}")
    print("  To review and approve:")
    print(f"      python -m src.approve --client {failure.provider_id}")
    print(f"      python -m src.approve --client {failure.provider_id} --approve-all")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m src.run",
        description="Reconcile a provider's General Ledger against its Trial Balance.",
    )
    parser.add_argument(
        "--client",
        required=True,
        help="provider identifier, matching a file in config/ (e.g. CLIENT_A)",
    )
    parser.add_argument(
        "--no-llm",
        action="store_true",
        help=(
            "resolve columns without a model. Tiers 1 and 2 still run and the "
            "pipeline still reaches a correct result; more columns go to review."
        ),
    )
    args = parser.parse_args(argv)

    llm = None if args.no_llm else StubMappingLLM()

    try:
        reconcile(args.client, OUT_DIR, llm=llm)
    except ReviewRequired as review:
        # Not a failure. The run stopped because a person has to decide
        # something, and it says what and how.
        report_review(review)
        return REVIEW_EXIT
    except (
        FileNotFoundError,
        FormatDetectionError,
        MappingError,
        ControlTotalError,
        ControlArithmeticError,
        json.JSONDecodeError,
    ) as failure:
        # Reported as what it is, at the stage it happened. Nothing is written
        # and nothing is guessed at to keep the run going.
        print(f"RUN FAILED  {type(failure).__name__}: {failure}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
