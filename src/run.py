"""Pipeline driver. Phase 1.

    python -m src.run --client CLIENT_A

    def main(argv=None) -> int
    def reconcile(client_id, out_dir) -> RunOutcome
    def load_dataset(dataset_key, path, config, model)
            -> tuple[DatasetOutcome, list]
    def new_run_id(started_at) -> str
    def sha256_of(path) -> str

Sequence: register sources, apply the static mapping, validate, find
duplicates, run controls, write reports, print the run summary.

One codebase, one configuration file per provider. The provider identifier
selects its config, its two source files and its mapping table; nothing about a
provider is written here.

A run that finds exceptions still exits 0. A reconciliation difference is an
output, not a failure, and a driver that returned non-zero for finding one
would be reporting the presence of evidence as an error. The exit code is about
whether the pipeline ran: a source that cannot be read, a column that cannot be
mapped, or a control total that does not hold all halt it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import yaml

from .models import CanonicalGLLine, CanonicalTBRow, parse_money
from .s0_ingest import FormatDetectionError, read_rows, register_source
from .s1_mapping import MappingError, apply_static_mapping
from .s2_validate import ControlTotalError, assert_control_total, validate_rows
from .s3_dedupe import find_duplicates
from .s4_reconcile import ControlArithmeticError, run_controls
from .s7_report import DatasetOutcome, RunOutcome, print_run_summary, write_reports

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = REPO_ROOT / "config"
DATA_DIR = REPO_ROOT / "data"
REGISTRY_PATH = REPO_ROOT / "registry" / "mappings.json"
OUT_DIR = REPO_ROOT / "out"

# Phase 1 resolves columns from the static table in stage 1. The three-tier
# resolver, and the only place a model may ever be called, is Phase 2.
MAPPING_RESOLVER = "static (Phase 1)"


def new_run_id(started_at: datetime) -> str:
    """An identifier for one run: when it started, and which run it was.

    Stable for the whole run and written into all three artefacts, so a
    statement, a register and a log can be tied to each other and to nothing
    else.
    """
    stamp = started_at.strftime("%Y%m%dT%H%M%SZ")
    return f"RUN-{stamp}-{uuid4().hex[:6]}"


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_dataset(
    dataset_key: str,
    path: Path,
    config: dict[str, Any],
    model: type,
) -> tuple[DatasetOutcome, list[Any]]:
    """Stages 0 to 2 for one file: register, map, validate.

    Returns the record of what happened to the file and the accepted records.
    """
    source = register_source(path, config)
    headers, raw_rows = read_rows(source)
    mapping = apply_static_mapping(headers, dataset_key)

    # Only the columns the file actually carries, renamed to canonical fields.
    # Values are untouched; parsing them is stage 2's decision to make.
    mapped_rows = [
        {
            canonical: raw_row[column]
            for column, canonical in mapping.items()
            if column in raw_row
        }
        for raw_row in raw_rows
    ]

    accepted, quarantined = validate_rows(
        mapped_rows,
        model,
        source,
        date_formats=config["parsing"]["date_formats"],
    )
    return (
        DatasetOutcome(
            dataset_key=dataset_key,
            source=source,
            mapping=mapping,
            rows_received=len(raw_rows),
            rows_accepted=len(accepted),
            quarantined=quarantined,
        ),
        accepted,
    )


def reconcile(client_id: str, out_dir: Path) -> RunOutcome:
    """Run the whole pipeline for one provider."""
    started_at = datetime.now(timezone.utc)
    run_id = new_run_id(started_at)

    config_path = CONFIG_DIR / f"{client_id.lower()}.yaml"
    if not config_path.is_file():
        raise FileNotFoundError(f"no configuration for {client_id} at {config_path}")
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))

    registry = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))

    # -- stages 0 to 2 ------------------------------------------------------
    gl_dataset, gl_lines = load_dataset(
        f"{client_id}_GL",
        DATA_DIR / f"{client_id.lower()}_gl.csv",
        config,
        CanonicalGLLine,
    )
    tb_dataset, tb_rows = load_dataset(
        f"{client_id}_TB",
        DATA_DIR / f"{client_id.lower()}_tb.csv",
        config,
        CanonicalTBRow,
    )
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
        # Nothing in this phase can call a model: stage 1 resolves from the
        # static table and no module imports a client. The counter is read off
        # the run like every other figure, and in this phase it reads zero.
        model_calls=0,
        datasets=datasets,
        gl_lines=gl_lines,
        tb_rows=tb_rows,
        exceptions=exceptions,
        summaries=summaries,
        decisions=decisions,
    )

    # -- stage 7 ------------------------------------------------------------
    written = write_reports(outcome, out_dir)
    print_run_summary(outcome)
    print(f"  {'Artefacts':<15}: " + ", ".join(str(path.relative_to(REPO_ROOT)) for path in written))
    return outcome


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
    args = parser.parse_args(argv)

    try:
        reconcile(args.client, OUT_DIR)
    except (
        FileNotFoundError,
        FormatDetectionError,
        MappingError,
        ControlTotalError,
        ControlArithmeticError,
    ) as failure:
        # Reported as what it is, at the stage it happened. Nothing is written
        # and nothing is guessed at to keep the run going.
        print(f"RUN FAILED  {type(failure).__name__}: {failure}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
