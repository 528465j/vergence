"""Stage 2 — Structural and type validation. Deterministic. Phase 1.

Quarantine rather than coerce. A failing row is written out with its failing
rule identifier and its original raw values preserved.

    def validate_rows(raw_rows, model) -> tuple[list, list]

Assert on every run:  rows_received == rows_accepted + rows_quarantined
If that identity fails the run is invalid and must halt.
"""

raise NotImplementedError("Phase 1")
