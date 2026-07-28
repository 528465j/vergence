"""Stage 0 — Ingestion and registration. Deterministic. Phase 1.

SHA-256 every arriving file and record filename, byte size, receipt
timestamp, client, engagement and period. Detect encoding and delimiter
rather than assuming UTF-8 and comma. Raw files are never modified.

    def register_source(path, client_id, engagement_id, period) -> SourceFile
    def sniff_format(path, encodings, delimiters) -> tuple[str, str]
"""

raise NotImplementedError("Phase 1")
