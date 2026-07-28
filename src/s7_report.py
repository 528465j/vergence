"""Stage 7 — Output, documentation and audit trail. Deterministic. Phase 1.

Three artefacts:
  1. Reconciliation statement — value in scope, value agreed, exceptions by
     category and by value.
  2. Exception register — one row per ExceptionRecord.
  3. Run log — run id, timestamp, config version, registry version, source
     file hashes, and every counter.

    def write_reports(run_id, exceptions, counters, out_dir) -> None
    def print_run_summary(run_id, counters) -> None
"""

raise NotImplementedError("Phase 1")
