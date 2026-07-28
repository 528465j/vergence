# Vergence
### Automated Data Quality and Reconciliation Pipeline

A controls-first pipeline that prepares financial ledger data for analysis. It
ingests a General Ledger and a Trial Balance from providers whose file formats
and column names differ, resolves them to a canonical schema, validates,
deduplicates, reconciles the two sources against each other, and emits an
exception register.

*Vergence* is the measure of how far two lines are from meeting. That is the
only quantity this system computes: the distance between what one source says
and what an independent source says about the same figure.

**This is a scoped prototype running against synthetic data.** It demonstrates
an architecture; it is not a production system and must never be described as
one.

## Architecture — seven stages, one module each

| Module | Stage | Classification |
|---|---|---|
| `src/s0_ingest.py` | 0 — Ingestion and registration | Deterministic |
| `src/s1_mapping.py` | 1 — Schema resolution and canonical mapping | **AI-proposed, human-gated** |
| `src/s2_validate.py` | 2 — Structural and type validation | Deterministic |
| `src/s3_dedupe.py` | 3 — Deduplication | Deterministic, human adjudication |
| `src/s4_reconcile.py` | 4 — The reconciliation engine | Deterministic |
| `src/s7_report.py` | 7 — Output, documentation and audit trail | Deterministic |
| `src/run.py` | Pipeline driver | Deterministic |
| `src/approve.py` | The human review gate for stage 1 | Deterministic |

Stage 5 (PDF journal extraction) and Stage 6 (exception narrative) are
deliberately out of scope. Do not add them.

## Hard rules

1. **`src/s1_mapping.py` is the only module permitted to call a language
   model.** Every other module is pure Python. If a task appears to need a
   model outside that module, the design is wrong — stop and say so.
2. **Money uses `decimal.Decimal`, never `float`.** Binary floating point
   introduces differences that look like reconciliation breaks but are not.
   Parse with `Decimal(str(value))` and quantise to 2 places.
3. **Exceptions are outputs, not failures.** A reconciliation difference is
   recorded, quantified, classified and evidenced. It is never silently
   corrected, netted off, or adjusted away.
4. **Validation quarantines, it does not coerce.** A row that fails its schema
   is written to a quarantine list with the failing rule identifier and its
   original raw values preserved.
5. **Nothing is deleted.** Every duplicate, proven or suspected, is flagged
   with a reversible decision record; the row itself stays in the population.
   Two identical rows can be entirely legitimate — a recurring daily bank fee,
   for example — which is why the decision is recorded rather than applied.
6. **Raw data is immutable.** Source files are hashed on arrival and read-only
   thereafter. All transformation produces new artefacts.
7. **Thresholds live in `config/`, never in code.** Materiality tolerance is
   audit-team configuration, not an engineering decision.
8. **The pipeline runs correctly with no model attached.** `resolve_columns`
   takes `llm=None` by default; unresolved columns route to the review queue.
9. **Stage 3 flags duplicates and records a reversible decision.** It returns
   the population unchanged, and every control in Stage 4 reads the source as
   delivered. Removing a row must never silently change what another control
   reports.

## Conventions

- Debits and credits are separate, both-positive columns.
- Closing balance = opening + period debits − period credits, applied
  identically to every account. Credit-balance accounts therefore show
  negative closing balances. This is intended.
- A clean Trial Balance has closing balances summing to exactly `0.00`.
- Financial year 2026 runs 2025-07-01 to 2026-06-30 (Australian FY).
- Control identifiers are `C1`–`C6` and `DEDUPE`. See the `recon-controls`
  skill for their definitions.

## Environment

Python 3.10+ in a virtual environment at `.venv`. Activate it before running
anything:

```bash
source .venv/bin/activate
python -m src.run --client CLIENT_A
python -m src.approve --client CLIENT_A --approve-all
python -m pytest -q
```

A run exits 0 for a completed reconciliation, 1 for a pipeline that could not
run, and 2 when stage 1 left columns for a person to decide. Approval is its
own command so the moment someone takes responsibility for a mapping appears
in shell history as its own act.

## Writing rules for this repository

This repository is public and permanent. Everything committed to it is read by
strangers with no context.

1. Every file describes the system and nothing else. No organisation, person
   or external process is named anywhere — code, comments, commit messages,
   README or documentation alike.
2. Claim only what the code does. A README that promises more than the source
   delivers is the one failure this project cannot survive, given that its
   subject is verifying claims against evidence.
3. Write for a reader who arrives in 2028 with no memory of why it was built.
   Avoid dates, deadlines and words that fix it to a moment.

## Out of scope

No user interface. No database. No cloud deployment. No PDF or OCR path. No
real client data of any kind, ever.
