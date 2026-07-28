"""Canonical models. Phase 1.

Every record entering the pipeline is validated into one of these shapes or
quarantined by stage 2 with its raw values intact. Money is Decimal, never
float.

    class CanonicalGLLine     one General Ledger line
    class CanonicalTBRow      one Trial Balance row
    class MappingProposal     one proposed source column to canonical field
    class ExceptionRecord     one finding from one control
    class ControlSummary      what one control did, found or not
    class SourceFile          a file as it arrived, and how it was read
    class DuplicateDecision   a reversible record about a duplicate group

    def parse_money(value) -> Decimal
    def parse_posting_date(value, info) -> date
    def parse_line_number(value) -> int
    def severity_for(value, tolerance, *, unbalanced_journal=False,
                     affects_balance=True) -> Severity

severity_for lives here, beside the Severity enumeration it applies, so that
two findings of the same size are graded identically whichever stage found
them. The tolerance is always the caller's to supply from the client config.

Two decisions in this file carry weight.

MappingProposal.canonical_field is a Literal over the canonical enumeration, so
a proposal naming a field that does not exist cannot be constructed at all.
The enumeration is enforced by the type system rather than requested in a
prompt, which is the difference between a constraint and an instruction the
proposer is free to ignore.

Validators reject; they do not coerce. Each parse failure raises against a
named rule identifier and stage 2 quarantines the row whole. Nothing here is
defaulted, substituted or repaired: a blank amount is not 0.00, an unreadable
date is not today, and a row arriving short a column fails rather than having
the gap filled.

Rule identifiers raised by this module, or mapped from a structural failure by
stage 2:

    V-REQUIRED          a field the model needs is absent from the row
    V-EMPTY             an identifying field is present but blank
    V-UNEXPECTED-FIELD  the row carries a field the model does not define
    V-TYPE              a field arrived as a type it cannot be read from
    V-DATE              a date matched none of the configured formats
    V-DECIMAL           a monetary value could not be read as a decimal
    V-INTEGER           a line number is not a positive whole number
    V-SIGN              a movement column that must be positive is negative
    V-SCHEMA            any other structural failure

Three rules are deliberately absent, because each is a finding rather than a
failure. Enforcing any of them here would quarantine the row and the control
that exists to report it would have nothing left to find:

    the financial year boundary                 C4 reports a posting outside
                                                the period
    uniqueness of (journal_id, line_no)         DEDUPE reports a repeated pair
    opening + debits - credits = closing        C2 and C6 report a Trial
                                                Balance that does not agree
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Annotated, Any, Literal, get_args

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, ValidationInfo
from pydantic_core import PydanticCustomError

# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------

CanonicalField = Literal[
    # General Ledger
    "entity",
    "account_code",
    "account_name",
    "posting_date",
    "period",
    "journal_id",
    "line_no",
    "description",
    "debit",
    "credit",
    "currency",
    "source_system",
    # Trial Balance. entity, account_code and account_name are shared with the
    # General Ledger and are not repeated.
    "opening_balance",
    "period_debits",
    "period_credits",
    "closing_balance",
]

# The same enumeration as data, derived from the Literal rather than typed out
# a second time, so a field can never exist in one and not the other.
CANONICAL_FIELDS: tuple[str, ...] = get_args(CanonicalField)

ControlId = Literal["C1", "C2", "C3", "C4", "C5", "C6", "DEDUPE"]
Severity = Literal["HIGH", "MEDIUM", "LOW", "INFO"]
DuplicateKind = Literal["EXACT", "SUSPECTED"]

# A control either ran or was skipped, and a skipped control says why. There is
# no third state: a control that could not be reached did not run.
ControlStatus = Literal["RUN", "SKIPPED"]

# Where a mapping came from. STATIC is the Phase 1 table in stage 1: a mapping
# resolved from it must say so rather than claim a tier it did not come from.
MappingTier = Literal["REGISTRY", "SYNONYM", "MODEL", "STATIC"]

CENTS = Decimal("0.01")


# ---------------------------------------------------------------------------
# Parsers
#
# Each returns the parsed value or raises. None of them has a fallback branch,
# because a fallback is how a bad value becomes a plausible one.
# ---------------------------------------------------------------------------


def parse_money(value: Any) -> Decimal:
    """Read a monetary amount as Decimal, quantised to two places.

    Decimal(str(value)) is the mandated route. Going through str() is what
    keeps binary floating point out: a float parsed directly by Decimal carries
    its full binary expansion and manufactures sub-cent differences that look
    exactly like reconciliation breaks.

    NaN and infinity are rejected explicitly. Both are accepted by the Decimal
    constructor and neither is an amount of money.
    """
    if isinstance(value, bool):
        # bool is a subclass of int; True would otherwise arrive as 1.00.
        raise PydanticCustomError("V-DECIMAL", "a monetary amount cannot be a boolean")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise PydanticCustomError(
            "V-DECIMAL",
            "{value} cannot be read as a monetary amount",
            {"value": repr(value)},
        ) from None
    if not parsed.is_finite():
        raise PydanticCustomError(
            "V-DECIMAL",
            "{value} is not a finite amount",
            {"value": str(parsed)},
        )
    try:
        return parsed.quantize(CENTS, rounding=ROUND_HALF_UP)
    except InvalidOperation:
        raise PydanticCustomError(
            "V-DECIMAL",
            "{value} carries more digits than a monetary amount can hold",
            {"value": str(parsed)},
        ) from None


def parse_posting_date(value: Any, info: ValidationInfo) -> date:
    """Read a date against the formats supplied in the validation context.

    The formats come from the parsing block of the client config and are tried
    in the order given there. If none matches, the value is rejected: guessing
    a format is how 03/04 becomes March in one file and April in the next.

    With no formats configured, a text date is rejected rather than assumed to
    be ISO. The pipeline reads its date formats from configuration or it does
    not read dates at all.
    """
    if isinstance(value, datetime):
        raise PydanticCustomError(
            "V-DATE",
            "a posting date carries no time of day; {value} is a datetime",
            {"value": value.isoformat()},
        )
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        raise PydanticCustomError(
            "V-DATE",
            "a date must arrive as text; this arrived as {kind}",
            {"kind": type(value).__name__},
        )

    formats = tuple((info.context or {}).get("date_formats") or ())
    for date_format in formats:
        try:
            return datetime.strptime(value, date_format).date()
        except ValueError:
            continue

    if not formats:
        raise PydanticCustomError(
            "V-DATE",
            "no date formats are configured, so {value} cannot be read",
            {"value": value},
        )
    raise PydanticCustomError(
        "V-DATE",
        "{value} matches none of the configured formats: {formats}",
        {"value": value, "formats": ", ".join(formats)},
    )


def severity_for(
    value: Decimal,
    tolerance: Decimal,
    *,
    unbalanced_journal: bool = False,
    affects_balance: bool = True,
) -> Severity:
    """Apply the severity scale to one finding, mechanically.

        HIGH    the difference exceeds materiality, or a journal does not
                balance
        MEDIUM  below materiality but affects a reported balance
        LOW     presentational or completeness matters with no balance impact
        INFO    recorded for the audit trail with no action implied

    Read top to bottom, first match wins, and the tolerance is the caller's to
    supply from the client config. Severity is a consequence of the arithmetic
    and never a judgement made control by control, so that two findings of the
    same size are graded the same way whichever control found them.

    INFO is on the scale and no control in the current register produces one.
    Nothing here invents an occasion for it.
    """
    if unbalanced_journal or value > tolerance:
        return "HIGH"
    if affects_balance:
        return "MEDIUM"
    return "LOW"


def parse_line_number(value: Any) -> int:
    """Read a line number as a positive whole number.

    A value carrying a fractional part is rejected rather than truncated:
    line 1.5 is not line 1, it is a file that needs looking at.
    """
    if isinstance(value, bool):
        raise PydanticCustomError("V-INTEGER", "a line number cannot be a boolean")
    if isinstance(value, int):
        number = value
    else:
        try:
            number = int(str(value))
        except (ValueError, TypeError):
            raise PydanticCustomError(
                "V-INTEGER",
                "{value} is not a whole number",
                {"value": repr(value)},
            ) from None
    if number < 1:
        raise PydanticCustomError(
            "V-INTEGER",
            "line numbers start at 1; this row carries {value}",
            {"value": number},
        )
    return number


# ---------------------------------------------------------------------------
# Field types
#
# NonEmptyText is for fields that identify a record or make it interpretable:
# an account with no code, or an amount with no currency, cannot be reconciled
# against anything. Descriptive fields are plain str, because a ledger line
# with a blank narrative is ordinary, not defective.
# ---------------------------------------------------------------------------

NonEmptyText = Annotated[str, Field(min_length=1)]
PostingDate = Annotated[date, BeforeValidator(parse_posting_date)]
LineNumber = Annotated[int, BeforeValidator(parse_line_number)]

# Signed: a credit-balance account closes negative under the sign convention.
Money = Annotated[Decimal, BeforeValidator(parse_money)]

# Unsigned: debits and credits arrive in separate, both-positive columns, so a
# negative movement is a file that does not match its declared convention.
Movement = Annotated[Decimal, BeforeValidator(parse_money), Field(ge=0)]

RowIndex = Annotated[int, Field(ge=0)]


class _SourceDerived(BaseModel):
    """A record read from a source file.

    Frozen, because raw data is immutable and a record derived from it is
    evidence rather than a working variable. Unknown fields are refused: a
    field the schema does not define is a mapping that went wrong, not a bonus.
    """

    # str_strip_whitespace is stated rather than left to the default, because
    # silently trimming a value is a coercion and this schema does not coerce.
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=False)


class _Adjudicable(BaseModel):
    """A record a human acts on.

    Not frozen: a disposition is set by a reviewer after the fact. Everything
    else about it is as strict as the frozen records.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)


# ---------------------------------------------------------------------------
# Canonical records
# ---------------------------------------------------------------------------


class CanonicalGLLine(_SourceDerived):
    """One General Ledger line, resolved to the canonical schema.

    Nothing here tests the financial year boundary or the uniqueness of
    (journal_id, line_no). A posting outside the period and a repeated line
    number are both structurally valid rows, and both are findings for a
    control to report rather than reasons to set the row aside.
    """

    entity: NonEmptyText
    account_code: NonEmptyText
    account_name: str
    posting_date: PostingDate
    period: NonEmptyText
    journal_id: NonEmptyText
    line_no: LineNumber
    description: str
    debit: Movement
    credit: Movement
    currency: NonEmptyText
    source_system: str


class CanonicalTBRow(_SourceDerived):
    """One Trial Balance row, resolved to the canonical schema.

    closing_balance is carried as delivered and is never re-derived from the
    other three columns. Whether opening plus movement equals closing is what
    C2 and C6 exist to answer; recomputing it here would answer it by erasing
    the question.
    """

    entity: NonEmptyText
    account_code: NonEmptyText
    account_name: str
    opening_balance: Money
    period_debits: Movement
    period_credits: Movement
    closing_balance: Money


# ---------------------------------------------------------------------------
# Pipeline records
# ---------------------------------------------------------------------------


class MappingProposal(_Adjudicable):
    """One proposed resolution of a source column to a canonical field.

    canonical_field is the enumeration itself, not a string that resembles it.
    A proposal naming a field that does not exist cannot be constructed, so it
    can never reach the review queue, be approved by mistake, or be written to
    the registry.
    """

    source_column: NonEmptyText
    canonical_field: CanonicalField
    confidence: Annotated[float, Field(ge=0.0, le=1.0)]
    source_tier: MappingTier
    rationale: NonEmptyText


class ExceptionRecord(_Adjudicable):
    """One finding from one control.

    An exception is an output. It is recorded, quantified, classified and
    evidenced, and it is never netted off or adjusted away.
    """

    run_id: NonEmptyText
    control_id: ControlId
    severity: Severity
    entity: NonEmptyText
    account_code: NonEmptyText | None
    journal_id: NonEmptyText | None
    # The monetary size of the difference, quantised to two places.
    value: Money
    # How many source rows are implicated.
    record_count: Annotated[int, Field(ge=0)]
    # The value compared against the tolerance in the client config. Both
    # outcomes are recorded; an immaterial difference is still a difference.
    above_materiality: bool
    # One line naming the specific rows or totals the finding rests on.
    evidence: NonEmptyText
    # OPEN until a human sets it otherwise. The only default in this file, and
    # it defaults to the state that requires someone to look.
    disposition: str = "OPEN"


class ControlSummary(BaseModel):
    """What one control did, whether or not it found anything.

    A control that finds nothing still returns a summary, so silence from a
    control is never ambiguous: the register shows it ran, over how many rows,
    and raised none. A control that was skipped says why in its own words
    rather than being absent and leaving the reader to guess.

    rows_tested counts the source rows the control examined. A control reading
    both populations counts both, because that is what it looked at.

    Frozen and closed to unknown fields like every other record here: a summary
    is a statement about a run that has already happened.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=False)

    run_id: NonEmptyText
    control_id: ControlId
    status: ControlStatus
    rows_tested: Annotated[int, Field(ge=0)]
    exceptions_raised: Annotated[int, Field(ge=0)]
    # Why a control was skipped. None when it ran; stated, never blank, when it
    # did not.
    reason: str | None
    # One line naming what else the control measured: a total it cross-checked
    # its own arithmetic against, or a limb of its test that is computed and
    # reported without being raised as a finding. None where a control measured
    # nothing beyond what its exceptions already say, and stated either way.
    detail: str | None


class SourceFile(_SourceDerived):
    """A file as it arrived, and the format it was read with.

    sha256 is the file's identity. Every artefact the run produces can be tied
    back through it to the exact bytes it was derived from.
    """

    path: Path
    filename: NonEmptyText
    sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    byte_size: Annotated[int, Field(ge=0)]
    received_at: datetime
    provider: NonEmptyText
    engagement: NonEmptyText
    period: NonEmptyText
    encoding: NonEmptyText
    # Exactly one character: the csv reader accepts nothing else.
    delimiter: Annotated[str, Field(min_length=1, max_length=1)]


class DuplicateDecision(_Adjudicable):
    """A reversible record of what was decided about a duplicate group.

    Two identical rows can be entirely legitimate, a recurring daily fee for
    example, which is why a decision is recorded rather than applied. The rows
    stay in the population; only the decision is written.

    row_indices holds at least two entries. A duplicate group of one is not a
    duplicate group.
    """

    decision_id: NonEmptyText
    kind: DuplicateKind
    row_indices: Annotated[list[RowIndex], Field(min_length=2)]
    decided_by: NonEmptyText
    decided_at: datetime
    disposition: str = "OPEN"
    # Nothing is deleted, so every decision can be undone. The flag is carried
    # on the record so a decision that was not reversible could not pass itself
    # off as one.
    reversible: bool = True
