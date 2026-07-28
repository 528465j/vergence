---
name: recon-controls
description: Canonical definitions of the GL-to-TB reconciliation controls C1-C6 and DEDUPE, the ExceptionRecord shape, and the severity scale. Use when writing, reviewing, testing or explaining any reconciliation control, exception record, materiality threshold, or control-total assertion in this repository.
---

# Reconciliation controls

Apply these definitions exactly. Do not invent additional controls or rename
existing ones.

## Control register

| ID | Name | Test | Exception raised when |
|---|---|---|---|
| `C1` | Journal balance | Sum of debits equals sum of credits, per `journal_id` and in aggregate | Any journal fails to self-balance |
| `C2` | GL-to-TB agreement | Closing balance derived from GL lines equals the TB closing balance, per account | Absolute difference exceeds the configured tolerance |
| `C3` | Account completeness | Every account in the TB appears in the GL and every account in the GL appears in the TB | An account is present in one source and absent from the other |
| `C4` | Period cutoff | Every posting date falls within the configured financial year | A posting is dated before or after the period |
| `C5` | Rollforward | Prior-year closing balance equals current-year opening balance, per account | Opening balance disagrees with prior closing |
| `C6` | TB internal balance | TB closing balances sum to zero | The Trial Balance does not internally balance |
| `DEDUPE` | Duplicate detection | Exact hash of (entity, account_code, posting_date, journal_id, line_no, debit, credit, description); and suspected duplicates sharing amount, date and account under different journal identifiers | An exact or suspected duplicate is found |

`C5` returns status `SKIPPED` with the reason `no prior period loaded` when no
prior-year Trial Balance is supplied. It is implemented, not omitted.

## ExceptionRecord fields

```
run_id            str        the run that produced this record
control_id        str        C1 | C2 | C3 | C4 | C5 | C6 | DEDUPE
severity          str        HIGH | MEDIUM | LOW | INFO
entity            str
account_code      str | None
journal_id        str | None
value             Decimal    the monetary size of the difference, 2dp
record_count      int        how many source rows are implicated
above_materiality bool       value compared against the configured tolerance
evidence          str        one line naming the specific rows or totals
disposition       str        OPEN by default; set only by a human reviewer
```

## Severity

- `HIGH` — the difference exceeds materiality, or a journal does not balance.
- `MEDIUM` — below materiality but affects a reported balance.
- `LOW` — presentational or completeness matters with no balance impact.
- `INFO` — recorded for the audit trail with no action implied.

Differences below the configured tolerance are recorded as immaterial. They
are recorded, not ignored, and never auto-adjusted.

## Standing rules

1. A control reports. It never repairs, nets off, or adjusts a balance.
2. Materiality tolerance is read from the client config. Never hard-code it.
3. All monetary comparison uses `Decimal`, quantised to 2 places.
4. Every control emits `ExceptionRecord`s and a per-control summary of rows
   tested, so a control that finds nothing is still visible as having run.
5. The control total `rows_received == rows_accepted + rows_quarantined` is
   asserted on every run. If it fails, the run is invalid and must halt.
