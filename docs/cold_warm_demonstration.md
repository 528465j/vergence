# Cold to warm: a recorded demonstration

Every command below was run against this repository, and every figure is copied
from its real output. Nothing is illustrative.

**The model tier ran as `StubMappingLLM`: a deterministic stand-in that returns
proposals from a fixed table.** It makes no network call, needs no key and
costs nothing. That is deliberate — it is what makes this demonstration free,
offline and reproducible by anyone who clones the repository and follows the
steps in order. Nothing here is a live model call, and the run summary names
the class that ran so no reading of it can suggest otherwise.

What the sequence shows: the first run of a provider cannot resolve every
column on its own and stops for a person; approving those columns records them;
the second run resolves the same file entirely from that record, without asking
the model anything; and the reconciliation is unchanged by which tier did the
resolving.

---

## 1. The registry is empty

It ships empty, so the first run of any provider is a cold one.

```console
$ cat registry/mappings.json
{
  "registry_version": 1,
  "description": "Approved column mappings, keyed by client. Written back to by the human review gate. Empty at first run by design.",
  "clients": {}
}
```

---

## 2. Cold run

Nothing is known about this provider's columns, so seven of the General Ledger's
twelve resolve from the shared synonym list, the model proposes the other five,
and the run stops before the controls rather than reconciling a file it has only
half read.

```console
$ python -m src.run --client CLIENT_A
REVIEW REQUIRED  6 column(s) need a decision before CLIENT_A can be reconciled.
  Mapping        : GL  tier1=0 tier2=7 tier3=5 review=5
                   TB  tier1=0 tier2=6 tier3=1 review=1
                                                       | model calls: 2 (StubMappingLLM)
  GL  5 awaiting: Co Code, Per, Ln, Ccy, Src
      Co Code       -> entity (0.88, tier 3)
      Per           -> period (0.91, tier 3)
      Ln            -> line_no (0.94, tier 3)
      Ccy           -> currency (0.96, tier 3)
      Src           -> source_system (0.89, tier 3)
      unresolved canonical fields: entity, period, line_no, currency, source_system
  TB  1 awaiting: Co Code
      Co Code       -> entity (0.88, tier 3)
      unresolved canonical fields: entity
  Queue written to out/review_queue.json
  To review and approve:
      python -m src.approve --client CLIENT_A
      python -m src.approve --client CLIENT_A --approve-all

$ echo $?
2
```

Exit 2 is its own outcome, distinct from 0 for a completed reconciliation and 1
for a pipeline that could not run: work waiting on a person is not a failure.

The queue names the model that proposed, the gate it was measured against, and
one record per column:

```console
$ python -c "
import json; q = json.load(open('out/review_queue.json'))
print(json.dumps({k: q[k] for k in ('provider','registry_version','confidence_gate','model','review_items')}, indent=2))
print('gl review:', json.dumps(q['datasets']['gl']['review'][0], indent=2))
"
{
  "provider": "CLIENT_A",
  "registry_version": 1,
  "confidence_gate": 0.85,
  "model": "StubMappingLLM",
  "review_items": 6
}
gl review: {
  "source_column": "Co Code",
  "canonical_field": "entity",
  "confidence": 0.88,
  "source_tier": 3,
  "rationale": "column heads a two-character company code"
}
```

---

## 3. List the queue, approve nothing

Listing is the default and writes nothing, so reading what is waiting can never
be mistaken for agreeing to it — the registry's checksum is the same before and
after.

```console
$ md5sum registry/mappings.json
2533dbde4704b65ab61160f540f08115  registry/mappings.json

$ python -m src.approve --client CLIENT_A
REVIEW QUEUE  CLIENT_A  |  run RUN-20260728T161803Z-e22649  |  6 awaiting
  Proposed by StubMappingLLM at a confidence gate of 0.85
  GL  5 awaiting, 7 already settled
      Co Code       entity          0.88  tier 3
        column heads a two-character company code
      Per           period          0.91  tier 3
        values match the FY2026 period label
      Ln            line_no         0.94  tier 3
        small ascending integers restarting per journal
      Ccy           currency        0.96  tier 3
        three-letter ISO currency codes
      Src           source_system   0.89  tier 3
        constant ERP system identifier across rows
  TB  1 awaiting, 6 already settled
      Co Code       entity          0.88  tier 3
        column heads a two-character company code

  Nothing was written. To approve:
      python -m src.approve --client CLIENT_A --approve-all
      python -m src.approve --client CLIENT_A --approve "<column>"

$ md5sum registry/mappings.json
2533dbde4704b65ab61160f540f08115  registry/mappings.json
```

Every queued column carries the confidence and the reason it was proposed, so
the person approving is reading an argument rather than a verdict.

---

## 4. Approve

Approval is its own command, so the moment someone takes responsibility for a
mapping appears in shell history as its own act; what gets written is the
complete settled mapping, not only the columns the model proposed.

```console
$ python -m src.approve --client CLIENT_A --approve-all
APPROVED  CLIENT_A
  GL   12 columns written  tier1=0 tier2=7 tier3=5   approved_by: deterministic 7 · human 5
  TB    7 columns written  tier1=0 tier2=6 tier3=1   approved_by: deterministic 6 · human 1
  Registry: registry/mappings.json  (19 columns across gl, tb)
  The next run of CLIENT_A resolves these at tier 1, with no model call.
```

Each entry records which tier reached it and who it is owed to, so a synonym
match is never later mistaken for a decision somebody made:

```console
$ python -c "
import json; r = json.load(open('registry/mappings.json'))
gl = r['clients']['CLIENT_A']['gl']
print(json.dumps({'approved_at': gl['approved_at'],
                  'columns': dict(list(gl['columns'].items())[:2])}, indent=2))
"
{
  "approved_at": "2026-07-28T16:18:36.900699Z",
  "columns": {
    "Co Code": {
      "canonical_field": "entity",
      "source_tier": 3,
      "confidence": 0.88,
      "approved_by": "human",
      "rationale": "column heads a two-character company code"
    },
    "Nominal Code": {
      "canonical_field": "account_code",
      "source_tier": 2,
      "confidence": 1.0,
      "approved_by": "deterministic",
      "rationale": "exact synonym match"
    }
  }
}
```

---

## 5. Warm run

The same two files, the same command, and now every column resolves at tier 1
from the record of what was approved — the model is attached and is asked
nothing.

```console
$ python -m src.run --client CLIENT_A
RUN RUN-20260728T161837Z-ab306f  |  provider=CLIENT_A  engagement=FY2026-AUDIT  period=FY2026
  Sources        : 2 files, SHA-256 recorded
  Rows           : received 59 | accepted 59 | quarantined 0   [control total OK]
  Mapping        : GL  tier1=12 tier2=0 tier3=0 review=0
                   TB  tier1=7 tier2=0 tier3=0 review=0
                                                       | model calls: 0 (StubMappingLLM)
  Controls run   : 7  (C5 SKIPPED - no prior period loaded)
  Exceptions     : 9  |  above materiality 7 | below 2
  By control     : C1 2 · C2 3 · C3 1 · C4 1 · C6 1 · DEDUPE 1
  Duration       : 0.013 s
  Artefacts      : out/reconciliation_statement.md, out/exception_register.csv, out/run_log.json, out/review_queue.json

$ echo $?
0
```

---

## 6. A second provider, different vocabulary

This step did not run as the sequence intended, and is recorded as it happened:
`CLIENT_B` has a General Ledger in `data/` but no Trial Balance, so stage 0
halts on the missing file before stage 1 reports anything.

```console
$ python -m src.run --client CLIENT_B
RUN FAILED  FileNotFoundError: no source file at /workspaces/vergence/data/client_b_tb.csv

$ echo $?
1

$ ls data/
client_a_gl.csv
client_a_tb.csv
client_b_gl.csv
defects_manifest.json
```

Calling the resolver directly on the file that does exist shows what the step
was for: a provider whose headers share not one name with `CLIENT_A`, resolving
nine of twelve deterministically against the same synonym list, with the
registry still holding only `CLIENT_A`.

```console
$ python - <<'PY'
import yaml
from pathlib import Path
from src.s0_ingest import read_rows, register_source
from src.s1_mapping import StubMappingLLM, load_registry, load_synonyms, resolve_columns

config = yaml.safe_load(Path("config/client_b.yaml").read_text())
registry = load_registry()
print("registry holds:", list(registry["clients"]))

source = register_source(Path("data/client_b_gl.csv"), config)
headers, rows = read_rows(source)
print("CLIENT_B General Ledger headers:")
print(" ", ", ".join(headers))

mapping, resolved, review = resolve_columns(
    headers, rows, config["client_id"], "gl", registry,
    synonyms=load_synonyms(), gate=config["mapping"]["confidence_gate"],
    llm=StubMappingLLM(),
)
tiers = {n: sum(1 for p in resolved + review if p.source_tier == n) for n in (1, 2, 3)}
print(f"\nGL  tier1={tiers[1]} tier2={tiers[2]} tier3={tiers[3]} review={len(review)}")
for p in resolved:
    print(f"    tier {p.source_tier}  {p.source_column:<14} -> {p.canonical_field}")
for p in review:
    print(f"    tier {p.source_tier}  {p.source_column:<14} -> {p.canonical_field}  "
          f"({p.confidence:.2f}, awaiting a decision)")
PY
registry holds: ['CLIENT_A']
CLIENT_B General Ledger headers:
  Entity_ID, GL_Acct, GL_Acct_Desc, TransDate, FiscalPeriod, Batch_ID, LineNum, Description, Debit_Amt, Credit_Amt, Currency, SourceSystem

GL  tier1=0 tier2=9 tier3=3 review=3
    tier 2  GL_Acct        -> account_code
    tier 2  GL_Acct_Desc   -> account_name
    tier 2  TransDate      -> posting_date
    tier 2  Batch_ID       -> journal_id
    tier 2  LineNum        -> line_no
    tier 2  Description    -> description
    tier 2  Debit_Amt      -> debit
    tier 2  Credit_Amt     -> credit
    tier 2  Currency       -> currency
    tier 3  Entity_ID      -> entity  (0.97, awaiting a decision)
    tier 3  FiscalPeriod   -> period  (0.97, awaiting a decision)
    tier 3  SourceSystem   -> source_system  (0.98, awaiting a decision)
```

`tier1=0` is the part worth reading twice: `CLIENT_A` is approved and warm, and
none of it carries over — a registry entry is scoped to the provider it was
approved for, so one provider's decisions can never resolve another's columns.

---

## 7. Reset

Discarding the registry returns the repository to the state at step 1, and the
cold run reproduces exactly, line for line.

```console
$ git checkout registry/mappings.json
$ cat registry/mappings.json
{
  "registry_version": 1,
  "description": "Approved column mappings, keyed by client. Written back to by the human review gate. Empty at first run by design.",
  "clients": {}
}

$ python -m src.run --client CLIENT_A
REVIEW REQUIRED  6 column(s) need a decision before CLIENT_A can be reconciled.
  Mapping        : GL  tier1=0 tier2=7 tier3=5 review=5
                   TB  tier1=0 tier2=6 tier3=1 review=1
                                                       | model calls: 2 (StubMappingLLM)
  ...

$ echo $?
2
```

The two cold runs, either side of an approval and a reset, differ only in their
run identifier:

```console
$ diff <(sed -E 's/RUN-[0-9TZ]+-[0-9a-f]+/RUN-ID/' first_cold.txt) \
       <(sed -E 's/RUN-[0-9TZ]+-[0-9a-f]+/RUN-ID/' second_cold.txt) && echo identical
identical
```

(`diff` prints nothing when two files match, so the `echo` is what confirms it
ran and found no difference.)

---

## What the three figures say

| | cold | warm |
|---|---:|---:|
| Model calls | 2 | 0 |
| Columns awaiting a decision | 6 | 0 |
| Exceptions raised | — | 9 |

**Model calls 2 then 0.** The model is asked once per file, and only about
columns two deterministic tiers could not settle. Once those columns are
approved they resolve from the registry forever, so the cost of the model tier
falls to nothing on the second run and stays there. It is an efficiency layer,
not a dependency.

**Review items 6 then 0.** Six columns went to a person because nothing could
resolve them deterministically and it was the first mapping for the provider.
That number goes to zero by someone deciding, not by the threshold moving.

**Exceptions 9 then 9.** The cold run raised none because it never reached the
controls. The nine come from the warm run — and they are the same nine, with
the same seven above materiality and two below, that the pipeline produced when
stage 1 resolved columns from a hand-written static table instead of these three
tiers. Verified by comparing the registers rather than the counts:

```console
$ diff phase1_exception_register.csv warm_exception_register.csv \
    && echo "IDENTICAL to the Phase 1 static-mapping register"
IDENTICAL to the Phase 1 static-mapping register
```

Both files are the register with the run identifier normalised, the first taken
from a checkout of the commit before the resolver was wired in.

That is the claim the demonstration exists to support. Mapping decides how
columns are read. It does not decide what the controls find.
