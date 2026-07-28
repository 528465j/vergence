"""Stage 1 — Schema resolution and canonical mapping.

The only module permitted to call a language model. In Phase 1 it does not:
resolution comes from a static table, and the three-tier resolver below is a
seam that raises rather than a stub that pretends.

    Tier 1  registry exact match            deterministic, free, instant
    Tier 2  synonym / fuzzy match           deterministic
    Tier 3  model proposal                  only what tiers 1-2 could not resolve
    Gate    low-confidence or first-time    -> human review queue

    def apply_static_mapping(headers, dataset_key) -> dict[str, str]      Phase 1
    def resolve_columns(headers, sample_rows, client_id, registry, llm=None)
            -> list[MappingProposal]                                      Phase 2

The registry and the synonym file are left untouched by Phase 1. An empty
registry and a deliberately incomplete synonym list are the starting conditions
the three-tier resolver exists to demonstrate; seeding either would remove the
thing being demonstrated and leave a resolver with nothing left to resolve.

`llm=None` is the default and must stay that way. With no model attached the
pipeline still runs to a correct result; it simply routes more columns to the
review queue. Approving a proposal writes it to registry/mappings.json, after
which it resolves at Tier 1 forever.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from .models import CANONICAL_FIELDS, MappingProposal


class MappingError(RuntimeError):
    """A source file cannot be resolved to the canonical schema."""


class UnknownDatasetError(MappingError):
    """The dataset key has no entry in the static table."""


class UnmappedColumnsError(MappingError):
    """One or more source columns are absent from the static table."""


# ---------------------------------------------------------------------------
# The Phase 1 static table
#
# One entry per dataset, source column to canonical field. Every provider names
# the same field differently, which is the whole reason stage 1 exists: the
# General Ledger below is the same ledger under two vocabularies.
# ---------------------------------------------------------------------------

STATIC_MAPPINGS: dict[str, dict[str, str]] = {
    "CLIENT_A_GL": {
        "Co Code": "entity",
        "Nominal Code": "account_code",
        "Nominal Name": "account_name",
        "Posting Dt": "posting_date",
        "Per": "period",
        "Jnl Ref": "journal_id",
        "Ln": "line_no",
        "Narrative": "description",
        "Amount DR": "debit",
        "Amount CR": "credit",
        "Ccy": "currency",
        "Src": "source_system",
    },
    "CLIENT_A_TB": {
        "Co Code": "entity",
        "Nominal Code": "account_code",
        "Nominal Name": "account_name",
        "Op Bal": "opening_balance",
        "Per DR": "period_debits",
        "Per CR": "period_credits",
        "Cl Bal": "closing_balance",
    },
    "CLIENT_B_GL": {
        "Entity_ID": "entity",
        "GL_Acct": "account_code",
        "GL_Acct_Desc": "account_name",
        "TransDate": "posting_date",
        "FiscalPeriod": "period",
        "Batch_ID": "journal_id",
        "LineNum": "line_no",
        "Description": "description",
        "Debit_Amt": "debit",
        "Credit_Amt": "credit",
        "Currency": "currency",
        "SourceSystem": "source_system",
    },
}


def _check_static_table() -> None:
    """Check the table against the canonical enumeration at import.

    A hand-written table is the one place a canonical field name can be
    misspelled without a type ever seeing it. Two columns pointing at the same
    canonical field is the other way the table can be wrong: one would
    overwrite the other and the loss would be silent.
    """
    for dataset_key, table in STATIC_MAPPINGS.items():
        unknown = sorted(set(table.values()) - set(CANONICAL_FIELDS))
        if unknown:
            raise MappingError(
                f"{dataset_key} maps to fields that are not in the canonical "
                f"schema: {', '.join(unknown)}"
            )
        repeated = sorted(field for field, n in Counter(table.values()).items() if n > 1)
        if repeated:
            raise MappingError(
                f"{dataset_key} maps more than one source column to "
                f"{', '.join(repeated)}"
            )


_check_static_table()


# ---------------------------------------------------------------------------
# Phase 1 resolution
# ---------------------------------------------------------------------------


def apply_static_mapping(headers: Iterable[str], dataset_key: str) -> dict[str, str]:
    """Resolve a file's headers to canonical fields from the static table.

    Returns source column to canonical field, in the order the columns appear
    in the file, describing this file rather than the table it came from.

    A header the table does not know is an error naming every such column, not
    a column quietly dropped. A dropped column would produce records that
    validate cleanly and reconcile against nothing.
    """
    try:
        table = STATIC_MAPPINGS[dataset_key]
    except KeyError:
        raise UnknownDatasetError(
            f"{dataset_key} is not a known dataset; the static table holds "
            f"{', '.join(sorted(STATIC_MAPPINGS))}"
        ) from None

    headers = list(headers)
    unmapped = [header for header in headers if header not in table]
    if unmapped:
        raise UnmappedColumnsError(
            f"{dataset_key}: {len(unmapped)} column(s) absent from the static "
            f"table: {', '.join(repr(header) for header in unmapped)}. Phase 1 "
            "resolves columns from the static table alone; the three-tier "
            "resolver and its review queue are Phase 2."
        )
    return {header: table[header] for header in headers}


# ---------------------------------------------------------------------------
# Phase 2 resolution
# ---------------------------------------------------------------------------


def resolve_columns(
    headers: Sequence[str],
    sample_rows: Sequence[Mapping[str, Any]],
    client_id: str,
    registry: Mapping[str, Any],
    llm: Any = None,
) -> list[MappingProposal]:
    """Resolve headers through the registry, then synonyms, then a model.

    Whatever the first two tiers settle is never sent to the third, and
    anything the third proposes below the configured confidence gate goes to a
    human before it is used. `llm=None` stays the default: with no model
    attached this resolves fewer columns and queues more of them, and still
    reaches a correct result.
    """
    raise NotImplementedError("Phase 2")
