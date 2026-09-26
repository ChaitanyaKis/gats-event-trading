"""Content-addressed, immutable store for raw payloads.

Each payload is saved once, gzip-compressed, under its SHA-256. Identical
payloads deduplicate automatically. Nothing is ever modified or deleted:
the raw store is what lets us re-run fixed parsers over history.
"""

from __future__ import annotations

import gzip
import hashlib
import os
import tempfile
from pathlib import Path


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class RawStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def relative_path(self, doc_id: str) -> Path:
        return Path(doc_id[:2]) / doc_id[2:4] / f"{doc_id}.gz"

    def put(self, data: bytes) -> tuple[str, Path]:
        """Store ``data`` and return ``(doc_id, relative_path)``. Idempotent."""
        doc_id = sha256_hex(data)
        rel = self.relative_path(doc_id)
        target = self.root / rel
        if target.exists():
            return doc_id, rel
        target.parent.mkdir(parents=True, exist_ok=True)
        # Write to a temp file then rename, so a crash never leaves a half-written blob.
        fd, tmp_name = tempfile.mkstemp(dir=target.parent, suffix=".tmp")
        try:
            with (
                os.fdopen(fd, "wb") as raw_file,
                gzip.GzipFile(fileobj=raw_file, mode="wb", mtime=0) as gz,
            ):
                gz.write(data)
            os.replace(tmp_name, target)
        except BaseException:
            Path(tmp_name).unlink(missing_ok=True)
            raise
        return doc_id, rel

    def get(self, doc_id: str) -> bytes:
        path = self.root / self.relative_path(doc_id)
        with gzip.open(path, "rb") as gz:
            data = gz.read()
        if sha256_hex(data) != doc_id:
            raise ValueError(f"raw blob {doc_id} is corrupt (hash mismatch)")
        return data
