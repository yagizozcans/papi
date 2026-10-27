"""Content-addressed, write-once raw object store.

Bytes live at ``$PAPI_HOME/raw/{sha[0:2]}/{sha}``; metadata in ``raw_object`` (one row per distinct
content) and ``fetch_log`` (one row per fetch). Same bytes fetched twice → one object, two fetch rows.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import Connection, select
from sqlalchemy.dialects.sqlite import insert

from papi.core import db
from papi.core.config import get_settings
from papi.core.ids import uuid7
from papi.core.schema import fetch_log, raw_object, source
from papi.core.timeutil import now_utc


class UnknownSourceError(LookupError):
    pass


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def path_for(sha: str) -> Path:
    """Absolute file path for a sha256 hex digest."""
    if len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha):
        raise ValueError(f"not a sha256 hex digest: {sha!r}")
    return get_settings().paths.raw / sha[:2] / sha


def storage_uri(sha: str) -> str:
    """Path relative to ``$PAPI_HOME`` stored in ``raw_object.storage_uri``."""
    return f"raw/{sha[:2]}/{sha}"


def source_id_for(conn: Connection, source_code: str) -> str:
    sid = conn.execute(select(source.c.id).where(source.c.code == source_code)).scalar_one_or_none()
    if sid is None:
        raise UnknownSourceError(f"unknown source code {source_code!r} (run `papi init`)")
    return sid


def _write_file(sha: str, data: bytes) -> None:
    target = path_for(sha)
    if target.exists():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o444)
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def put_bytes(
    data: bytes,
    *,
    source_code: str,
    url: str | None = None,
    http_status: int | None = None,
    content_type: str | None = None,
    request_meta: dict[str, Any] | None = None,
    response_headers: dict[str, Any] | None = None,
    fetcher_version: str | None = None,
    started_at: datetime | None = None,
    conn: Connection | None = None,
) -> str:
    """Store ``data`` (idempotent) and log the fetch; returns the sha256 hex digest.

    With ``conn`` the rows are written in the caller's transaction (caller commits); otherwise in a
    transaction of its own. The file is written before the rows, so a committed row always has bytes.
    """
    sha = sha256_hex(data)
    _write_file(sha, data)
    if conn is None:
        with db.transaction() as tx:
            _record(
                tx,
                sha,
                data,
                source_code,
                url,
                http_status,
                content_type,
                request_meta,
                response_headers,
                fetcher_version,
                started_at,
            )
    else:
        _record(
            conn,
            sha,
            data,
            source_code,
            url,
            http_status,
            content_type,
            request_meta,
            response_headers,
            fetcher_version,
            started_at,
        )
    return sha


def _record(
    conn: Connection,
    sha: str,
    data: bytes,
    source_code: str,
    url: str | None,
    http_status: int | None,
    content_type: str | None,
    request_meta: dict[str, Any] | None,
    response_headers: dict[str, Any] | None,
    fetcher_version: str | None,
    started_at: datetime | None,
) -> None:
    sid = source_id_for(conn, source_code)
    now = now_utc()
    conn.execute(
        insert(raw_object)
        .values(
            sha256=sha,
            source_id=sid,
            url=url,
            http_status=http_status,
            request_meta=request_meta,
            response_headers=response_headers,
            fetched_at=now,
            content_type=content_type,
            byte_size=len(data),
            storage_uri=storage_uri(sha),
        )
        .on_conflict_do_nothing(index_elements=[raw_object.c.sha256])
    )
    conn.execute(
        fetch_log.insert().values(
            id=uuid7(),
            source_id=sid,
            raw_sha256=sha,
            url=url,
            http_status=http_status,
            fetcher_version=fetcher_version,
            started_at=started_at or now,
            finished_at=now,
            outcome="ok",
        )
    )


def log_fetch_failure(
    *,
    source_code: str,
    url: str | None,
    outcome: str,
    error: str | None = None,
    http_status: int | None = None,
    fetcher_version: str | None = None,
    started_at: datetime | None = None,
    conn: Connection | None = None,
) -> str:
    """Record a failed fetch (no bytes) in ``fetch_log``; returns the fetch_log id.

    ``outcome``: ``http_error`` | ``timeout`` | ``blocked`` | ``error``.
    """
    fid = uuid7()

    def _do(c: Connection) -> None:
        now = now_utc()
        c.execute(
            fetch_log.insert().values(
                id=fid,
                source_id=source_id_for(c, source_code),
                raw_sha256=None,
                url=url,
                http_status=http_status,
                fetcher_version=fetcher_version,
                started_at=started_at or now,
                finished_at=now,
                outcome=outcome,
                error=error,
            )
        )

    if conn is None:
        with db.transaction() as tx:
            _do(tx)
    else:
        _do(conn)
    return fid


def exists(sha: str) -> bool:
    return path_for(sha).exists()


def get_bytes(sha: str, *, verify: bool = False) -> bytes:
    """Read stored bytes; ``FileNotFoundError`` if absent; ``ValueError`` on hash mismatch if ``verify``."""
    data = path_for(sha).read_bytes()
    if verify and sha256_hex(data) != sha:
        raise ValueError(f"raw object {sha} is corrupted")
    return data
