# Vergence
### Automated Data Quality and Reconciliation Pipeline

Two independent sources are supposed to describe the same figures. Often they
do not. This pipeline finds every place where they disagree, measures the
disagreement, explains where it came from, and never quietly resolves it.

The worked domain is financial: a General Ledger of individual transactions and
a Trial Balance summarising the same period. The pattern is general — any two
systems that should agree, arriving from providers who each name their columns
differently.

An LLM is used in exactly one stage, for exactly one task, behind a human
approval gate. Every figure the system reports is arithmetic.

## The name

Vergence is the measure of how far two lines are from meeting. Convergence and
divergence are the same word with the sign flipped, which is the point: a
difference between two sources is a quantity to be measured, not a fault to be
corrected. Where the vergence is zero the sources agree. Where it is not, that
is a finding, and the system's job is to size it and evidence it rather than
close it.

---

## The problem

Data arrives from many providers. Each one exports from a different system, so
the same field is called `Nominal Code` in one file, `GL_Acct` in another and
`Account_No` in a third. Amounts are inconsistent, rows are duplicated, dates
fall outside the period they claim to cover, and the summary does not always
agree with the detail it summarises.

The naive fix is a script per provider. That does not survive contact with
scale, because the number of scripts grows with the number of providers and
every one of them has to be maintained separately.

## What it does

| Stage | Module | Classification |
|---|---|---|
| 0 · Ingestion and registration | `src/s0_ingest.py` | Deterministic |
| 1 · Schema resolution and canonical mapping | `src/s1_mapping.py` | **LLM-assisted, human-gated** |
| 2 · Structural and type validation | `src/s2_validate.py` | Deterministic |
| 3 · Deduplication | `src/s3_dedupe.py` | Deterministic |
| 4 · Reconciliation controls | `src/s4_reconcile.py` | Deterministic |
| 7 · Reporting and audit trail | `src/s7_report.py` | Deterministic |

One codebase. Providers are added as configuration files, never as forks.

## Where a model is used, and where it is not

`src/s1_mapping.py` resolves incoming column names to the canonical schema in
three tiers: an exact match against a registry of previously approved mappings,
then deterministic synonym and fuzzy matching, and only then a model proposal
for whatever remains. Every proposal is constrained to the canonical field
enumeration by its type, carries a confidence score, and goes to a human
review queue. Approving one writes it to the registry, after which it resolves
deterministically forever.

`resolve_columns` takes `llm=None` by default. **The model is never required to
reach a result.** On a provider already in the registry it is not called at all.
On a new one, disconnecting it leaves exactly the same columns awaiting exactly
the same decision — a person composes the mapping rather than confirming a
drafted one. The model saves a reviewer time; nothing it proposes reaches a
reported figure without a person approving it first.

Nowhere else in the system may a model speak. Balancing debits against
credits, agreeing a summary to its detail and detecting duplicates are
arithmetic, and arithmetic does not need a model.

## Results

*(Filled in once the pipeline runs — planted defects detected, controls
exercised, and the change in model calls between a cold and a warm run.)*

## Design decisions

- **Money is `decimal.Decimal`, never `float`.** Binary floating point
  manufactures sub-cent differences that look exactly like genuine breaks.
- **Validation quarantines; it does not coerce.** A row that fails its schema
  is set aside with its failing rule and its original values intact.
- **Nothing is deleted.** Every duplicate, proven or suspected, is flagged with
  a reversible decision record and the row stays in the population, so removing
  a row can never silently change what another control reports.
- **Differences are outputs, not failures.** The system is judged on how well
  it evidences a difference, not on whether it made one disappear.
- **Thresholds live in configuration.** Materiality belongs to the people who
  own the judgement, not to the code.

## Running it

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python tools/make_synthetic_data.py

python -m src.run --client CLIENT_A          # first run stops for a decision
python -m src.approve --client CLIENT_A      # what needs deciding; writes nothing
python -m src.approve --client CLIENT_A --approve-all
python -m src.run --client CLIENT_A          # resolves from the registry, no model call

python -m pytest -q
```

The registry ships empty, so the first run of a provider has columns nothing
can resolve deterministically and stops rather than guessing. It exits 2 —
distinct from 0, a completed reconciliation, and from 1, a pipeline that could
not run — because work waiting on a person is not a failure.

## Scope and limitations

A scoped prototype against synthetic data, written to test whether the
architecture holds. Stages 5 (extracting fields from PDF source documents) and
6 (drafting narrative explanations of exception clusters) are designed but not
implemented. There is no user interface, no database and no deployment layer.
No real data of any kind has ever passed through it.

The model behind stage 1 is a deterministic stub. It proposes from a fixed
table rather than reasoning about a column, so a run is reproducible by anyone
who clones this repository and no key or network is needed to exercise the
path. The live client is written and left unwired; the seam it sits behind is
the point, not the model on the other side of it.

## Licence

MIT. See [LICENSE](LICENSE).
