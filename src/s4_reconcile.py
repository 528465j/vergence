"""Stage 4 — The reconciliation engine. Purely deterministic. Phase 1.

No language model appears anywhere in this module.

Controls C1 to C6 are defined in the recon-controls skill; implement them
exactly as specified there. Tolerance is read from the client config.

    def run_controls(gl_lines, tb_rows, config, prior_tb=None)
            -> list[ExceptionRecord]

The relationship C2 tests:
    opening + period debits - period credits = closing
    and the TB closing balance for an account must equal the balance derived
    by summing that account's GL transactions.
"""

raise NotImplementedError("Phase 1")
