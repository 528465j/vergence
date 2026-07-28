"""Canonical Pydantic models. Phase 1.

Every record entering the pipeline is coerced into one of these shapes or
quarantined. Money is Decimal, never float.

To implement:
    CanonicalGLLine   entity, account_code, account_name, posting_date,
                      period, journal_id, line_no, description, debit,
                      credit, currency, source_system
    CanonicalTBRow    entity, account_code, account_name, opening_balance,
                      period_debits, period_credits, closing_balance
    MappingProposal   source_column, canonical_field, confidence,
                      source_tier, rationale
    ExceptionRecord   see the recon-controls skill for the full field list

Two decisions that are load-bearing:
  * canonical_field is a Literal over the canonical enumeration, so a model
    cannot propose a field that does not exist.
  * Validators reject; they do not coerce. An unparseable date fails.
"""

raise NotImplementedError("Phase 1")
