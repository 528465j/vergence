"""Stage 1 — Schema resolution and canonical mapping.

The only module permitted to call a language model, and the only one that
knows a model exists. Nothing here calls one on its own: the caller supplies
the model or does not, and the resolver reaches tier 3 only for the columns
the two deterministic tiers left behind.

    Tier 1  registry exact match            deterministic, free, instant
    Tier 2  synonym / fuzzy match           deterministic
    Tier 3  model proposal                  only what tiers 1-2 could not resolve
    Gate    low-confidence or first-time    -> human review queue

    def resolve_columns(headers, sample_rows, provider_id, dataset, registry,
                        *, synonyms, gate, llm=None)
            -> tuple[dict[str, str], list[MappingProposal], list[MappingProposal]]

    def load_registry(path) / load_synonyms(path)      read paths
    def registry_entry(proposals, *, approved_at, approved, previous) -> dict
    def merged_registry(registry, provider_id, dataset, entry) -> dict
    def save_registry(registry, path) -> Path
    def proposal_model_for(dataset) -> type[MappingProposal]

    class StubMappingLLM       deterministic stand-in, the tested path
    class LiveMappingLLM       schema-constrained call to a real model, unwired

This is the only path from a source column to a canonical field. There is no
second table to resolve through, so what mapped a given run is never in
question: it was these three tiers, and the run states which one settled each
column.

A run reads the registry and never writes to it. Recording an approval is the
approval command's act alone, so nothing a pipeline does can widen what it is
allowed to resolve without asking.

The registry and the synonym file are left untouched. An empty registry and a
deliberately incomplete synonym list are the starting conditions the three-tier
resolver exists to demonstrate; seeding either would remove the thing being
demonstrated and leave a resolver with nothing left to resolve.

`llm=None` is the default and must stay that way. With no model attached the
pipeline still runs to a correct result; it simply routes more columns to the
review queue, each carrying no tier because no tier reached it. Approving a
proposal writes it to registry/mappings.json, after which it resolves at
Tier 1 forever.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Literal, Optional, Protocol, runtime_checkable

import yaml
from pydantic import BaseModel, ValidationError, create_model
from rapidfuzz import fuzz

from .models import (
    CANONICAL_FIELDS,
    CanonicalGLLine,
    CanonicalTBRow,
    MappingProposal,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
REGISTRY_PATH = REPO_ROOT / "registry" / "mappings.json"
SYNONYMS_PATH = REPO_ROOT / "config" / "synonyms.yaml"

# How many rows of the file the model is shown, and profiled over.
SAMPLE_ROWS = 20


class MappingError(RuntimeError):
    """A source file cannot be resolved to the canonical schema."""


class UnknownDatasetError(MappingError):
    """The dataset key has no entry in the static table."""


class RegistryError(MappingError):
    """The registry of approved mappings cannot be read as what it claims to be.

    A registry entry is a mapping a human approved. One that has been edited
    into a shape the schema does not recognise is a fault to report, not a
    finding to record: resolving around it would silently drop an approval
    someone made deliberately.
    """


class SynonymError(MappingError):
    """The synonym list names a field the dataset does not have."""


# ---------------------------------------------------------------------------
# The canonical enumeration, per dataset
#
# Derived from the canonical records rather than typed out again, so the fields
# offered to a proposer are exactly the fields the record will accept. A
# General Ledger has twelve, a Trial Balance seven, and the three they share
# are shared because they are the same field.
# ---------------------------------------------------------------------------

DATASET_FIELDS: dict[str, tuple[str, ...]] = {
    "gl": tuple(CanonicalGLLine.model_fields),
    "tb": tuple(CanonicalTBRow.model_fields),
}


def _check_dataset_fields() -> None:
    """Check each dataset's fields against the canonical enumeration at import."""
    for dataset, fields in DATASET_FIELDS.items():
        unknown = sorted(set(fields) - set(CANONICAL_FIELDS))
        if unknown:
            raise MappingError(
                f"the {dataset} record carries fields that are not in the "
                f"canonical schema: {', '.join(unknown)}"
            )


_check_dataset_fields()


_PROPOSAL_MODELS: dict[str, type[MappingProposal]] = {}


def proposal_model_for(dataset: str) -> type[MappingProposal]:
    """The proposal record whose canonical_field is this dataset's enumeration.

    A Trial Balance column cannot be proposed a General Ledger field, because
    the Literal is built from the dataset's own fields and the construction
    fails. The enumeration reaches the model as well, in the schema its answer
    is constrained to, but the schema is what was asked for and the type is
    what is enforced.
    """
    try:
        fields = DATASET_FIELDS[dataset]
    except KeyError:
        raise UnknownDatasetError(
            f"{dataset!r} is not a known dataset; stage 1 resolves "
            f"{', '.join(sorted(DATASET_FIELDS))}"
        ) from None

    model = _PROPOSAL_MODELS.get(dataset)
    if model is None:
        model = create_model(
            f"{dataset.upper()}MappingProposal",
            __base__=MappingProposal,
            __doc__=(
                f"One proposed resolution of a {dataset} column, narrowed to "
                f"the {len(fields)} fields that dataset defines."
            ),
            canonical_field=(Optional[Literal[fields]], ...),
        )
        _PROPOSAL_MODELS[dataset] = model
    return model


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------

_SEPARATORS = re.compile(r"[ _\-]+")


def normalise_header(header: str) -> str:
    """Lowercase a header and drop spaces, underscores and hyphens.

    `Co Code`, `co_code` and `CO-CODE` are one column named three ways. What
    survives normalisation is compared; what arrived is what gets recorded.
    """
    return _SEPARATORS.sub("", header).lower()


# ---------------------------------------------------------------------------
# Tier 1 — the registry read path
#
# The registry ships empty, so the first run against a provider is always a
# cold one and every column it cannot resolve deterministically is put to a
# person. The write path at the foot of this module is what makes the second
# run warm.
# ---------------------------------------------------------------------------


def load_registry(path: Path = REGISTRY_PATH) -> dict[str, Any]:
    """Read the registry of approved mappings."""
    registry = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(registry, dict):
        raise RegistryError(f"{path} does not hold a registry object")
    return registry


def _provider_entry(
    registry: Mapping[str, Any], provider_id: str, dataset: str
) -> Mapping[str, Any] | None:
    """The registry entry for one provider and dataset, or None if there is none."""
    clients = registry.get("clients") or {}
    if not isinstance(clients, Mapping):
        raise RegistryError("the registry's clients section is not an object")
    provider = clients.get(provider_id) or {}
    if not isinstance(provider, Mapping):
        raise RegistryError(f"the registry entry for {provider_id} is not an object")
    entry = provider.get(dataset)
    if entry is None:
        return None
    if not isinstance(entry, Mapping):
        raise RegistryError(
            f"the registry entry for {provider_id} {dataset} is not an object"
        )
    return entry


def has_registry_entry(
    registry: Mapping[str, Any], provider_id: str, dataset: str
) -> bool:
    """Whether this provider and dataset have been approved before.

    This is what the gate turns on. A provider seen for the first time has
    every model proposal reviewed however confident it is, because there is no
    prior approval for it to have been consistent with.
    """
    return _provider_entry(registry, provider_id, dataset) is not None


def _approved_columns(
    entry: Mapping[str, Any] | None, provider_id: str, dataset: str
) -> dict[str, Any]:
    """The approved columns held on one registry entry, source column keyed."""
    if entry is None:
        return {}
    columns = entry.get("columns") or {}
    if not isinstance(columns, Mapping):
        raise RegistryError(
            f"the approved columns for {provider_id} {dataset} are not an object"
        )
    return dict(columns)


def registry_columns(
    registry: Mapping[str, Any], provider_id: str, dataset: str
) -> dict[str, Any]:
    """The approved columns for one provider and dataset, source column keyed."""
    return _approved_columns(
        _provider_entry(registry, provider_id, dataset), provider_id, dataset
    )


def _registry_proposal(
    model: type[MappingProposal],
    header: str,
    column: Any,
    provider_id: str,
    dataset: str,
    approved_at: Any,
) -> MappingProposal:
    """Turn one approved registry column into a resolved proposal.

    Confidence is 1.0 and the tier is 1: this is an exact lookup of a decision
    already taken, not a judgement being made again. Everything the registry
    records about how the mapping was originally arrived at — who approved it,
    when, at which tier, and why — is carried into the rationale, so resolving
    from the registry never loses the account of where the mapping came from
    or lets a deterministic resolution pass itself off as a human decision.

    The approval date is held once on the entry, beside the columns it covers,
    and a column may carry its own where one was recorded.
    """
    if not isinstance(column, Mapping):
        raise RegistryError(
            f"the approved mapping for {provider_id} {dataset} column {header!r} "
            "is not an object"
        )
    approved_by = column.get("approved_by") or "an unrecorded approver"
    approved_on = column.get("approved_at") or approved_at or "an unrecorded date"
    original_tier = column.get("source_tier")
    stated = column.get("rationale")

    provenance = f"approved by {approved_by} on {approved_on}"
    if original_tier is not None:
        provenance += f" at tier {original_tier}"
    if stated:
        provenance += f": {stated}"

    try:
        return model(
            source_column=header,
            canonical_field=column.get("canonical_field"),
            confidence=1.0,
            source_tier=1,
            rationale=f"registry entry for {provider_id} {dataset}, {provenance}",
        )
    except ValidationError as failure:
        raise RegistryError(
            f"the approved mapping for {provider_id} {dataset} column {header!r} "
            f"is not a valid {dataset} mapping: {failure}"
        ) from None


# ---------------------------------------------------------------------------
# Tier 2 — synonyms, then fuzzy matching
# ---------------------------------------------------------------------------


def load_synonyms(path: Path = SYNONYMS_PATH) -> dict[str, Any]:
    """Read the shared synonym list."""
    synonyms = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(synonyms, Mapping):
        raise SynonymError(f"{path} does not hold a synonym list")
    return dict(synonyms)


def synonym_index(synonyms: Mapping[str, Any], dataset: str) -> dict[str, str]:
    """Normalised synonym to canonical field, for one dataset.

    A term naming a field the dataset does not have is an error rather than a
    term quietly ignored: a synonym list that cannot resolve what it claims to
    resolve sends work to the model that tier 2 was meant to absorb.
    """
    fields = DATASET_FIELDS.get(dataset)
    if fields is None:
        raise UnknownDatasetError(
            f"{dataset!r} is not a known dataset; stage 1 resolves "
            f"{', '.join(sorted(DATASET_FIELDS))}"
        )

    block = synonyms.get(dataset) or {}
    if not isinstance(block, Mapping):
        raise SynonymError(f"the {dataset} section of the synonym list is not an object")

    index: dict[str, str] = {}
    for field, terms in block.items():
        if field not in fields:
            raise SynonymError(
                f"the synonym list maps {field!r} for {dataset}, which has no "
                f"such field; {dataset} carries {', '.join(fields)}"
            )
        for term in terms or ():
            normalised = normalise_header(str(term))
            claimed = index.get(normalised)
            if claimed is not None and claimed != field:
                raise SynonymError(
                    f"the {dataset} synonym {term!r} maps to both {claimed} and "
                    f"{field}; a term cannot resolve to two fields"
                )
            index[normalised] = field
    return index


@dataclass(frozen=True)
class _LexicalMatch:
    """The best lexical candidate for one header, matched or merely nearest."""

    field: str | None
    confidence: float
    term: str | None
    exact: bool


def _match_lexically(header: str, index: Mapping[str, str]) -> _LexicalMatch:
    """The best synonym for a header: an exact hit, or the nearest fuzzy one.

    An exact hit on the normalised header is confidence 1.0. Otherwise the
    closest term scores ratio/100, and is returned whether or not it clears the
    gate — a near miss that does not resolve is still worth telling a reviewer
    about. Ties keep the first candidate in file order, so the same file
    resolves the same way every run.
    """
    normalised = normalise_header(header)
    field = index.get(normalised)
    if field is not None:
        return _LexicalMatch(field=field, confidence=1.0, term=normalised, exact=True)

    best = _LexicalMatch(field=None, confidence=0.0, term=None, exact=False)
    for term, candidate in index.items():
        confidence = fuzz.ratio(normalised, term) / 100
        if confidence > best.confidence:
            best = _LexicalMatch(
                field=candidate, confidence=confidence, term=term, exact=False
            )
    return best


# ---------------------------------------------------------------------------
# Column profiles
#
# What the model is told about a column beyond its name. Every figure here is
# counted off the rows the model is also shown, so nothing in the request
# asserts anything about data the model cannot see for itself.
# ---------------------------------------------------------------------------

ColumnKind = Literal["empty", "integer", "decimal", "date", "text"]

# Shapes, not parsing decisions. Stage 2 reads dates against the formats in the
# client config and rejects anything else; a profile only says what the values
# look like.
_DATE_SHAPES = ("%Y-%m-%d", "%d/%m/%Y", "%d-%b-%Y")


@dataclass(frozen=True)
class ColumnProfile:
    """What one column's values look like across the sampled rows."""

    column: str
    rows: int
    populated: int
    distinct: int
    kind: ColumnKind
    examples: tuple[str, ...]


def _looks_like(values: Sequence[str]) -> ColumnKind:
    """Classify a column's populated values by shape."""
    if not values:
        return "empty"
    if all(_is_integer(value) for value in values):
        return "integer"
    if all(_is_decimal(value) for value in values):
        return "decimal"
    if all(_is_dated(value) for value in values):
        return "date"
    return "text"


def _is_integer(value: str) -> bool:
    try:
        int(value)
    except ValueError:
        return False
    return True


def _is_decimal(value: str) -> bool:
    try:
        parsed = Decimal(value)
    except (InvalidOperation, ValueError):
        return False
    return parsed.is_finite()


def _is_dated(value: str) -> bool:
    for shape in _DATE_SHAPES:
        try:
            datetime.strptime(value, shape)
        except ValueError:
            continue
        return True
    return False


def profile_columns(
    headers: Iterable[str], rows: Sequence[Mapping[str, Any]], examples: int = 3
) -> dict[str, ColumnProfile]:
    """A profile per column, over exactly the rows supplied."""
    profiles: dict[str, ColumnProfile] = {}
    for header in headers:
        values = [
            str(row[header]).strip()
            for row in rows
            if header in row and row[header] is not None
        ]
        populated = [value for value in values if value]
        seen: list[str] = []
        for value in populated:
            if value not in seen:
                seen.append(value)
        profiles[header] = ColumnProfile(
            column=header,
            rows=len(rows),
            populated=len(populated),
            distinct=len(seen),
            kind=_looks_like(populated),
            examples=tuple(seen[:examples]),
        )
    return profiles


# ---------------------------------------------------------------------------
# Tier 3 — the model boundary
#
# One request per dataset, carrying only what tiers 1 and 2 could not resolve.
# The boundary is a protocol with two implementations so it can be swapped and
# tested: nothing below the protocol knows whether a model was reached.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MappingRequest:
    """Everything a proposer is given, and nothing it is not.

    proposal_model carries the dataset's enumeration as a type, so an
    implementation constructs its answers against the same constraint the
    resolver validates them with.
    """

    provider_id: str
    dataset: str
    canonical_fields: tuple[str, ...]
    headers: tuple[str, ...]
    sample_rows: tuple[Mapping[str, Any], ...]
    profiles: Mapping[str, ColumnProfile]
    proposal_model: type[MappingProposal]


@runtime_checkable
class MappingLLM(Protocol):
    """What stage 1 requires of a proposer.

    One call, one request, a proposal for each header it recognises. Returning
    nothing for a header is a valid answer and the honest one where the
    proposer does not recognise the column.
    """

    def propose(self, request: MappingRequest) -> list[MappingProposal]: ...


# The stub's fixed answers, keyed on the normalised header. Anything absent
# gets no proposal at all, so the stub never invents a recognition it does not
# have and the column reaches the review queue unresolved.
STUB_PROPOSALS: dict[str, tuple[str, float, str]] = {
    "cocode": ("entity", 0.88, "column heads a two-character company code"),
    "per": ("period", 0.91, "values match the FY2026 period label"),
    "ln": ("line_no", 0.94, "small ascending integers restarting per journal"),
    "ccy": ("currency", 0.96, "three-letter ISO currency codes"),
    "src": ("source_system", 0.89, "constant ERP system identifier across rows"),
    "entityid": ("entity", 0.97, "column heads a stable entity identifier"),
    "fiscalperiod": ("period", 0.97, "values match the FY2026 period label"),
    "sourcesystem": (
        "source_system",
        0.98,
        "constant ERP system identifier across rows",
    ),
}


class StubMappingLLM:
    """Deterministic stand-in. No network, no key, no cost.

    The tested path. A fixed table keyed on the normalised header, so a run is
    reproducible by anyone who clones the repository and the tier figures a
    test asserts are the same figures on every machine.

    A header the table does not hold gets no proposal, which is the same answer
    a model should give for a column it cannot read. A proposal for a field
    outside the dataset's enumeration is dropped rather than raised: the stub
    is a proposer, and a proposer offering the wrong field is answering badly,
    not breaking the pipeline.
    """

    def propose(self, request: MappingRequest) -> list[MappingProposal]:
        proposals: list[MappingProposal] = []
        for header in request.headers:
            answer = STUB_PROPOSALS.get(normalise_header(header))
            if answer is None:
                continue
            field, confidence, rationale = answer
            if field not in request.canonical_fields:
                continue
            proposals.append(
                request.proposal_model(
                    source_column=header,
                    canonical_field=field,
                    confidence=confidence,
                    source_tier=3,
                    rationale=rationale,
                )
            )
        return proposals


class LiveMappingLLM:
    """Schema-constrained call to a real model. Not wired in this task.

    The request shape is here and honest, and `propose` raises. Leaving the
    class present and unwired says what a live call would look like without
    claiming the pipeline makes one: no client is constructed, no key is read
    and no dependency on a model vendor is imported, so a checkout with no
    model available runs exactly as this one does.

    Wiring it means one request per dataset, constrained to a single tool whose
    schema is built from the dataset's own enumeration by `tool_schema` below,
    with the tool forced so the answer cannot arrive as prose. What comes back
    is then validated through `request.proposal_model` like any other proposal
    — the schema is what was asked for, and the type is what is enforced.
    """

    TOOL_NAME = "propose_column_mappings"

    def __init__(
        self,
        *,
        model: str = "claude-opus-5",
        max_tokens: int = 4096,
        client: Any = None,
    ) -> None:
        self.model = model
        self.max_tokens = max_tokens
        self.client = client

    def tool_schema(self, request: MappingRequest) -> dict[str, Any]:
        """The tool a live call would constrain the answer to.

        Both enumerations are closed: a proposal can only name a column that
        was actually sent and a field the dataset actually has. Confidence
        carries no numeric bounds here because the schema language does not
        enforce them; the 0.0 to 1.0 range is enforced on the way back in, by
        the field on the proposal.

        source_tier is absent by design. The model is never asked which tier it
        is — that is the resolver's fact about where the answer came from, and
        asking would let the answer misreport its own provenance.
        """
        return {
            "name": self.TOOL_NAME,
            "description": (
                "Propose a canonical field for each unresolved source column, "
                "or omit the column entirely if its values do not identify one."
            ),
            "strict": True,
            "input_schema": {
                "type": "object",
                "properties": {
                    "proposals": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "source_column": {
                                    "type": "string",
                                    "enum": list(request.headers),
                                },
                                "canonical_field": {
                                    "type": "string",
                                    "enum": list(request.canonical_fields),
                                },
                                "confidence": {"type": "number"},
                                "rationale": {"type": "string"},
                            },
                            "required": [
                                "source_column",
                                "canonical_field",
                                "confidence",
                                "rationale",
                            ],
                            "additionalProperties": False,
                        },
                    }
                },
                "required": ["proposals"],
                "additionalProperties": False,
            },
        }

    def propose(self, request: MappingRequest) -> list[MappingProposal]:
        raise NotImplementedError("not wired — the stub is the tested path")


# ---------------------------------------------------------------------------
# The resolver
# ---------------------------------------------------------------------------


def _unresolved_proposal(
    model: type[MappingProposal],
    header: str,
    nearest: _LexicalMatch,
    gate: float,
    reason: str,
) -> MappingProposal:
    """A queue entry for a column nothing resolved.

    No field, no tier and no confidence, because none was reached. The nearest
    lexical candidate is recorded where there was one, so a reviewer can see
    what the deterministic tiers came close to rather than only that they
    failed.
    """
    if nearest.field is None:
        account = "no synonym in the dataset's list came close"
    else:
        account = (
            f"nearest synonym {nearest.term!r} for {nearest.field} scored "
            f"{nearest.confidence:.2f}, below the {gate:.2f} gate"
        )
    return model(
        source_column=header,
        canonical_field=None,
        confidence=0.0,
        source_tier=None,
        rationale=f"unresolved: {account}; {reason}",
    )


def _model_proposals(
    llm: Any,
    model: type[MappingProposal],
    request: MappingRequest,
) -> dict[str, MappingProposal]:
    """The one model call, and the validation of what it returns.

    Every proposal is re-validated through the dataset's proposal record rather
    than trusted as it arrives, and its tier is set here rather than read off
    the answer. A proposal for a column that was not sent, or a second proposal
    for one that was, is an error: both mean the answer does not correspond to
    the question, and neither is something to resolve around.
    """
    proposals: dict[str, MappingProposal] = {}
    for proposal in llm.propose(request):
        record = (
            proposal.model_dump() if isinstance(proposal, BaseModel) else dict(proposal)
        )
        record["source_tier"] = 3
        try:
            checked = model.model_validate(record)
        except ValidationError as failure:
            raise MappingError(
                f"the model proposed a mapping that is not a valid "
                f"{request.dataset} mapping: {failure}"
            ) from None
        if checked.source_column not in request.headers:
            raise MappingError(
                f"the model proposed a mapping for {checked.source_column!r}, "
                "which was not among the columns it was sent"
            )
        if checked.source_column in proposals:
            raise MappingError(
                f"the model proposed two mappings for {checked.source_column!r}"
            )
        proposals[checked.source_column] = checked
    return proposals


def _contested(
    model: type[MappingProposal], proposal: MappingProposal, holder: str
) -> MappingProposal:
    """The same proposal, rerouted because another column already holds its field."""
    return model(
        source_column=proposal.source_column,
        canonical_field=proposal.canonical_field,
        confidence=proposal.confidence,
        source_tier=proposal.source_tier,
        rationale=(
            f"{proposal.rationale}; {holder!r} already resolves to "
            f"{proposal.canonical_field}, so both need adjudication"
        ),
    )


def resolve_columns(
    headers: Sequence[str],
    sample_rows: Sequence[Mapping[str, Any]],
    provider_id: str,
    dataset: str,
    registry: Mapping[str, Any],
    *,
    synonyms: Mapping[str, Any],
    gate: float,
    llm: Any = None,
) -> tuple[dict[str, str], list[MappingProposal], list[MappingProposal]]:
    """Resolve headers through the registry, then synonyms, then a model.

    Returns the resolved mapping, the proposals that resolved it, and the
    review queue, each in the order the columns appear in the file.

        Tier 1  exact lookup in the registry, keyed on provider and dataset.
                Confidence 1.0, tier 1.
        Tier 2  an exact synonym at 1.0, or the nearest fuzzy candidate scoring
                ratio/100 and resolving only at or above the gate. Tier 2.
        Tier 3  one call, carrying every header the first two tiers could not
                resolve, twenty sample rows and a profile per column. Tier 3.

    Whatever the first two tiers settle is never sent to the third. What the
    third proposes goes to a human when its confidence is below the gate, and
    also when the provider and dataset have no registry entry yet: a first
    mapping for a provider is reviewed however confident the proposal is,
    because there is no approval it can be consistent with. That clause is
    about the model's judgement — a synonym that matches exactly is a lexical
    fact rather than a judgement, and resolves without asking.

    `llm=None` is the default. With no model attached, tiers 1 and 2 run, every
    unresolved header enters the review queue carrying no tier, and this
    returns normally. It resolves fewer columns and queues more of them, and
    what it does resolve is resolved identically.

    Two columns cannot resolve to the same canonical field. The second is
    queued rather than allowed to overwrite the first, because one of the two
    is wrong and which one is a question for a human, not for whichever
    happened to be read last.
    """
    fields = DATASET_FIELDS.get(dataset)
    if fields is None:
        raise UnknownDatasetError(
            f"{dataset!r} is not a known dataset; stage 1 resolves "
            f"{', '.join(sorted(DATASET_FIELDS))}"
        )

    model = proposal_model_for(dataset)
    entry = _provider_entry(registry, provider_id, dataset)
    first_mapping = entry is None
    approved = _approved_columns(entry, provider_id, dataset)
    approved_at = entry.get("approved_at") if entry is not None else None
    index = synonym_index(synonyms, dataset)

    headers = list(headers)

    # -- tiers 1 and 2, column by column ------------------------------------
    deterministic: dict[str, MappingProposal] = {}
    nearest: dict[str, _LexicalMatch] = {}
    unresolved: list[str] = []

    for header in headers:
        column = approved.get(header)
        if column is not None:
            deterministic[header] = _registry_proposal(
                model, header, column, provider_id, dataset, approved_at
            )
            continue

        match = _match_lexically(header, index)
        nearest[header] = match
        if match.exact:
            deterministic[header] = model(
                source_column=header,
                canonical_field=match.field,
                confidence=1.0,
                source_tier=2,
                rationale="exact synonym match",
            )
        elif match.field is not None and match.confidence >= gate:
            deterministic[header] = model(
                source_column=header,
                canonical_field=match.field,
                confidence=match.confidence,
                source_tier=2,
                rationale=(
                    f"fuzzy match to synonym {match.term!r} for {match.field} "
                    f"scoring {match.confidence:.2f}, at or above the "
                    f"{gate:.2f} gate"
                ),
            )
        else:
            unresolved.append(header)

    # -- tier 3, once for the dataset ---------------------------------------
    proposed: dict[str, MappingProposal] = {}
    if unresolved and llm is not None:
        rows = tuple(sample_rows[:SAMPLE_ROWS])
        proposed = _model_proposals(
            llm,
            model,
            MappingRequest(
                provider_id=provider_id,
                dataset=dataset,
                canonical_fields=fields,
                headers=tuple(unresolved),
                sample_rows=rows,
                profiles=profile_columns(unresolved, rows),
                proposal_model=model,
            ),
        )

    # -- the gate -----------------------------------------------------------
    mapping: dict[str, str] = {}
    resolved: list[MappingProposal] = []
    review: list[MappingProposal] = []
    claimed: dict[str, str] = {}

    def settle(proposal: MappingProposal) -> None:
        field = proposal.canonical_field
        holder = claimed.get(field)
        if holder is not None:
            review.append(_contested(model, proposal, holder))
            return
        claimed[field] = proposal.source_column
        mapping[proposal.source_column] = field
        resolved.append(proposal)

    for header in headers:
        proposal = deterministic.get(header)
        if proposal is not None:
            settle(proposal)
            continue

        proposal = proposed.get(header)
        if proposal is None:
            review.append(
                _unresolved_proposal(
                    model,
                    header,
                    nearest.get(header, _LexicalMatch(None, 0.0, None, False)),
                    gate,
                    "no model attached"
                    if llm is None
                    else "the model returned no proposal for this column",
                )
            )
        elif proposal.confidence < gate:
            review.append(proposal)
        elif first_mapping:
            review.append(proposal)
        else:
            settle(proposal)

    return mapping, resolved, review


# ---------------------------------------------------------------------------
# The registry write path
#
# The counterpart of the read path above, and the only place an approval is
# recorded. It is driven by the approval command rather than by a run: a run
# reads the registry and never adds to it, so nothing a pipeline does can
# widen what it is allowed to resolve on its own.
# ---------------------------------------------------------------------------

# How a column came to be in the registry. Every entry says which, so a
# deterministic resolution is never misrepresented as a human decision, and a
# human decision never decays into one.
DETERMINISTIC = "deterministic"
HUMAN = "human"
MODEL = "model"


def approved_by_for(proposal: MappingProposal, *, approved: bool) -> str:
    """How a written entry was arrived at.

    Tiers 1 and 2 are deterministic whoever pressed approve: a registry lookup
    and a synonym match are facts about the file and the configuration, and
    stay facts when a person confirms one. Where a reviewer did adjudicate a
    deterministic mapping — two columns claiming one field is the case that
    reaches them — the rationale carried alongside says so.

    A tier 3 proposal a person approved is theirs. A tier 3 proposal that
    resolved without being approved — the provider was already known and the
    confidence cleared the gate — is the model's, and says so rather than
    borrowing either of the other two answers.
    """
    if proposal.source_tier in (1, 2):
        return DETERMINISTIC
    return HUMAN if approved else MODEL


def registry_entry(
    proposals: Sequence[MappingProposal],
    *,
    approved_at: str,
    approved: Iterable[str] = (),
    previous: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The registry entry for one provider and dataset.

    `proposals` is the complete settled mapping — everything the resolver
    settled, not only what the model proposed — because a warm run resolves
    entirely at tier 1 and can only do so if every column is recorded.
    `approved` names the columns a person approved in this act.

    A column that resolved at tier 1 is written back exactly as it was found.
    It came from the registry, so rewriting it as a fresh tier 1 entry would
    record the lookup rather than the decision, and a mapping a person
    approved would quietly become a deterministic one on the next approval.
    """
    approved = set(approved)
    previous = previous or {}
    columns: dict[str, Any] = {}

    for proposal in proposals:
        if proposal.canonical_field is None:
            raise MappingError(
                f"{proposal.source_column!r} has no proposed field and cannot be "
                "written to the registry"
            )
        carried = previous.get(proposal.source_column)
        if proposal.source_tier == 1 and isinstance(carried, Mapping):
            columns[proposal.source_column] = dict(carried)
            continue
        columns[proposal.source_column] = {
            "canonical_field": proposal.canonical_field,
            "source_tier": proposal.source_tier,
            "confidence": proposal.confidence,
            "approved_by": approved_by_for(
                proposal, approved=proposal.source_column in approved
            ),
            "rationale": proposal.rationale,
        }

    return {"approved_at": approved_at, "columns": columns}


def merged_registry(
    registry: Mapping[str, Any], provider_id: str, dataset: str, entry: Mapping[str, Any]
) -> dict[str, Any]:
    """The registry with one provider's dataset replaced, everything else kept.

    Returns a new registry rather than editing the one passed in. Every other
    provider, and this provider's other dataset, are carried through untouched:
    approving a Trial Balance must not disturb a General Ledger somebody
    approved last week.
    """
    merged = json.loads(json.dumps(registry))
    clients = merged.setdefault("clients", {})
    if not isinstance(clients, dict):
        raise RegistryError("the registry's clients section is not an object")
    provider = clients.setdefault(provider_id, {})
    if not isinstance(provider, dict):
        raise RegistryError(f"the registry entry for {provider_id} is not an object")
    provider[dataset] = dict(entry)
    return merged


def save_registry(registry: Mapping[str, Any], path: Path = REGISTRY_PATH) -> Path:
    """Write the registry, in the shape and formatting the read path expects."""
    with Path(path).open("w", encoding="utf-8") as handle:
        json.dump(registry, handle, indent=2)
        handle.write("\n")
    return Path(path)
