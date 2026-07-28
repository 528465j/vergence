"""Stage 2 — Structural and type validation. Deterministic. Phase 1.

Quarantine rather than coerce. A failing row is set aside whole, carrying the
rule that failed, the field it failed on, and its original raw values
unmodified. Nothing is repaired, defaulted or rounded into acceptability.

    def validate_rows(raw_rows, model, source, *, date_formats=None)
            -> tuple[list, list[dict]]
    def assert_control_total(rows_received, rows_accepted, rows_quarantined,
                             *, source=None) -> None

date_formats comes from the parsing block of the client config and is tried in
the order given there. It has no default worth the name: with none supplied a
text date is rejected rather than assumed to be ISO, because a date format is
configuration and not something this stage is entitled to invent.

Asserted on every run:  rows_received == rows_accepted + rows_quarantined
If that identity fails the run is invalid and must halt. assert_control_total
raises rather than asserting, so the check cannot be switched off with
python -O; stage 4 and the driver call it too.

What this stage does not test is as load-bearing as what it does. A posting
dated outside the financial year, a repeated (journal_id, line_no) and a Trial
Balance row whose closing balance does not follow from its movements are all
structurally valid rows. Each is the finding of a control — C4, DEDUPE, and C2
with C6 — and quarantining any of them here would remove the evidence the
control is supposed to report.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from pydantic import BaseModel, ValidationError

from .models import SourceFile

# Structural failures raised by the schema itself, mapped onto the repository's
# rule identifiers. Failures raised by the parsers in models.py already carry
# their rule identifier as the error type and pass through untouched.
RULE_FOR_ERROR_TYPE: dict[str, str] = {
    "missing": "V-REQUIRED",
    "extra_forbidden": "V-UNEXPECTED-FIELD",
    "string_too_short": "V-EMPTY",
    "string_type": "V-TYPE",
    "int_type": "V-TYPE",
    "bool_type": "V-TYPE",
    "bool_parsing": "V-TYPE",
    # The only lower bound on a canonical record is the both-positive
    # convention for the debit and credit movement columns.
    "greater_than_equal": "V-SIGN",
}

DEFAULT_RULE = "V-SCHEMA"


class ControlTotalError(RuntimeError):
    """rows_received != rows_accepted + rows_quarantined.

    Every row that arrives is either accepted or quarantined, so the identity
    holds by construction. If it ever does not, rows have gone missing between
    the file and the population and no figure computed downstream can be
    trusted. The run is invalid and must halt.
    """


# ---------------------------------------------------------------------------
# Failure description
# ---------------------------------------------------------------------------


def rule_for(error: Mapping[str, Any]) -> str:
    """The rule identifier a validation failure is recorded against."""
    error_type = str(error["type"])
    if error_type.startswith("V-"):
        return error_type
    return RULE_FOR_ERROR_TYPE.get(error_type, DEFAULT_RULE)


def field_for(error: Mapping[str, Any]) -> str | None:
    """The field a validation failure names, or None for a whole-row failure."""
    return ".".join(str(part) for part in error["loc"]) or None


def quarantine_entry(
    row_number: int,
    raw_row: Mapping[str, Any],
    error: ValidationError,
    source: SourceFile,
) -> dict[str, Any]:
    """Describe one rejected row.

    The row is reported once however many rules it broke, because the control
    total counts rows and not failures. The first failure is named at the top
    level and every failure is listed under `failures`, so nothing found is
    thrown away.

    `raw` is the row exactly as it was handed in: values untouched, right down
    to the whitespace and the unparseable date. It is copied so that nothing
    downstream can edit the evidence.
    """
    failures = [
        {
            "rule_id": rule_for(error_detail),
            "field": field_for(error_detail),
            "detail": error_detail["msg"],
        }
        for error_detail in error.errors(include_url=False)
    ]
    first = failures[0]
    return {
        "source_file": source.filename,
        "source_sha256": source.sha256,
        "row_number": row_number,
        "rule_id": first["rule_id"],
        "field": first["field"],
        "detail": first["detail"],
        "failures": failures,
        "raw": dict(raw_row),
    }


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate_rows(
    raw_rows: Iterable[Mapping[str, Any]],
    model: type[BaseModel],
    source: SourceFile,
    *,
    date_formats: Sequence[str] | None = None,
) -> tuple[list[BaseModel], list[dict[str, Any]]]:
    """Validate raw rows into canonical records, quarantining what fails.

    `raw_rows` are rows keyed by canonical field, values exactly as read from
    the file. `date_formats` come from the parsing block of the client config
    and are tried in the order given; with none supplied, a text date is
    rejected rather than assumed, because a date format is configuration and
    not something this stage is entitled to invent.

    Returns accepted model instances and quarantine entries. Row numbering is
    1-based over the rows handed in, which for a file read by stage 0 is the
    position after the header.
    """
    context = {"date_formats": tuple(date_formats or ())}
    accepted: list[BaseModel] = []
    quarantined: list[dict[str, Any]] = []

    # Counted as consumed rather than measured with len(), so the identity is
    # checked against what this loop actually saw and not against a second
    # reading of the input.
    rows_received = 0
    for row_number, raw_row in enumerate(raw_rows, start=1):
        rows_received += 1
        try:
            accepted.append(model.model_validate(dict(raw_row), context=context))
        except ValidationError as failure:
            quarantined.append(quarantine_entry(row_number, raw_row, failure, source))

    assert_control_total(rows_received, len(accepted), len(quarantined), source=source)
    return accepted, quarantined


def assert_control_total(
    rows_received: int,
    rows_accepted: int,
    rows_quarantined: int,
    *,
    source: SourceFile | None = None,
) -> None:
    """Halt unless rows_received == rows_accepted + rows_quarantined.

    Raised rather than asserted: a bare assert disappears under python -O, and
    a control that can be switched off from the command line is not a control.
    """
    if rows_received != rows_accepted + rows_quarantined:
        where = f" for {source.filename}" if source is not None else ""
        raise ControlTotalError(
            f"control total failed{where}: {rows_received} rows received, "
            f"{rows_accepted} accepted plus {rows_quarantined} quarantined "
            f"= {rows_accepted + rows_quarantined}. The run is invalid."
        )
