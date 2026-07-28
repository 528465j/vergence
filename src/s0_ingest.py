"""Stage 0 — Ingestion and registration. Deterministic. Phase 1.

SHA-256 every arriving file and record filename, byte size, receipt timestamp,
provider, engagement and period. Encoding and delimiter are detected against
the candidates in the parsing block of the client config rather than assumed to
be UTF-8 and comma.

    def register_source(path, config) -> SourceFile
    def sniff_format(path, encodings, delimiters) -> tuple[str, str]
    def read_rows(source) -> tuple[list[str], list[dict[str, Any]]]
    def digest_and_size(path) -> tuple[str, int]

read_rows returns Any rather than str per cell because a ragged row does not
give one: a short row carries None where the header promised a value, and a
long row puts its surplus in a list. Both are stage 2's to reject.

Files are opened read-only and never written to. The hash is taken before
anything interprets the contents, so the identity of the file is fixed before
any opinion is formed about what is in it.

Rows are read with the csv module, which returns every cell as text. That is
deliberate: a reader that infers types would parse account code 1100 as an
integer and 0.00 as a float, deciding for stage 2 what stage 2 exists to
decide.
"""

from __future__ import annotations

import csv
import hashlib
import io
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import SourceFile

READ_CHUNK = 65536


class FormatDetectionError(RuntimeError):
    """No configured encoding or delimiter reads the file.

    Raised rather than falling back to UTF-8 and comma. A fallback would let a
    file be read under a format nobody chose, and every value downstream would
    inherit that guess.
    """


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def digest_and_size(path: Path) -> tuple[str, int]:
    """SHA-256 of the file and its size, both taken from the same read."""
    digest = hashlib.sha256()
    byte_size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(READ_CHUNK), b""):
            digest.update(chunk)
            byte_size += len(chunk)
    return digest.hexdigest(), byte_size


def register_source(path: str | Path, config: Mapping[str, Any]) -> SourceFile:
    """Hash an arriving file, detect its format, and register it.

    Every field but the timestamp is a property of the file or of the
    engagement config. Nothing is defaulted here: a config missing client_id,
    engagement_id, period or its parsing block raises rather than registering a
    file under an assumed provider.
    """
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"no source file at {path}")

    sha256, byte_size = digest_and_size(path)

    parsing = config["parsing"]
    encoding, delimiter = sniff_format(path, parsing["encodings"], parsing["delimiters"])

    return SourceFile(
        path=path,
        filename=path.name,
        sha256=sha256,
        byte_size=byte_size,
        received_at=datetime.now(timezone.utc),
        # The provider is identified by the client_id of the engagement config.
        # One configuration file per provider; the code is never forked.
        provider=config["client_id"],
        engagement=config["engagement_id"],
        period=config["period"],
        encoding=encoding,
        delimiter=delimiter,
    )


# ---------------------------------------------------------------------------
# Format detection
# ---------------------------------------------------------------------------


def sniff_format(
    path: Path,
    encodings: Sequence[str],
    delimiters: Sequence[str],
) -> tuple[str, str]:
    """Return the first configured encoding that decodes the file, and the
    delimiter that splits it into a consistent table.

    Encodings are tried in configured order and the order is load-bearing:
    latin-1 decodes any byte sequence at all, so it can only ever be a last
    resort and never a first attempt.

    A delimiter qualifies when every row of the file splits into the same
    number of fields and that number is at least two. Where several qualify,
    the one producing the most columns wins, and ties go to the earlier entry
    in the config. A file that no candidate splits is not read under a guess;
    it is refused.
    """
    if not encodings:
        raise FormatDetectionError(f"{path.name}: no encodings are configured")
    if not delimiters:
        raise FormatDetectionError(f"{path.name}: no delimiters are configured")

    payload = path.read_bytes()
    if not payload:
        raise FormatDetectionError(f"{path.name} is empty")

    # The whole file is decoded, not a sample. A decoder that fails on the last
    # row has not read the file, and detection that stops early would not know.
    text = None
    encoding = ""
    for candidate in encodings:
        try:
            text = payload.decode(candidate)
        except UnicodeDecodeError:
            continue
        encoding = candidate
        break
    if text is None:
        raise FormatDetectionError(
            f"{path.name}: none of {', '.join(encodings)} decodes the file"
        )

    best_delimiter = ""
    best_width = 0
    for candidate in delimiters:
        if len(candidate) != 1:
            raise FormatDetectionError(
                f"{path.name}: {candidate!r} is not a single-character delimiter"
            )
        # StringIO with newline='' is the reader's contract: a newline inside a
        # quoted field belongs to the field, not to the table.
        rows = list(csv.reader(io.StringIO(text, newline=""), delimiter=candidate))
        populated = [row for row in rows if row]
        if not populated:
            continue
        width = len(populated[0])
        consistent = all(len(row) == width for row in populated)
        if consistent and width >= 2 and width > best_width:
            best_delimiter, best_width = candidate, width

    if not best_delimiter:
        raise FormatDetectionError(
            f"{path.name}: none of {', '.join(repr(d) for d in delimiters)} "
            "splits the file into a table with a consistent number of columns"
        )
    return encoding, best_delimiter


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def read_rows(source: SourceFile) -> tuple[list[str], list[dict[str, Any]]]:
    """Read a registered file as its header and its rows, every cell text.

    The file is opened read-only under the encoding and delimiter recorded at
    registration, so what is read is what was hashed.

    A well-formed row is text throughout. A row carrying fewer fields than the
    header keeps the absent keys with a value of None, which stage 2 rejects;
    the gap is never filled. A row carrying more lands its surplus under the
    key __extra__, which no canonical field targets, so it can never reach an
    accepted record unnoticed.
    """
    with source.path.open("r", encoding=source.encoding, newline="") as handle:
        reader = csv.DictReader(
            handle,
            delimiter=source.delimiter,
            restkey="__extra__",
            restval=None,
        )
        headers = list(reader.fieldnames or [])
        rows = [dict(row) for row in reader]
    return headers, rows
