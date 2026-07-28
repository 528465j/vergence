"""Stage 3 — Deduplication. Deterministic, human adjudication. Phase 1.

Exact duplicates: hash of entity, account_code, posting_date, journal_id,
line_no, debit, credit, description.

Suspected duplicates: same amount, date and account under different journal
identifiers.

    def find_duplicates(lines) -> tuple[list[ExceptionRecord], list[DuplicateDecision]]

Two identical rows can be entirely legitimate — a recurring daily bank fee,
for example. Duplicates are therefore flagged and recorded, never applied:
every one, exact or suspected, produces a reversible decision record for a
human to adjudicate. The line population is returned unchanged, so stage 4
reads the ledger exactly as delivered and removing a row can never silently
change what another control reports.
"""

raise NotImplementedError("Phase 1")
