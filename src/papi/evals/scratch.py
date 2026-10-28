"""Throw-away databases for evals, so suites never read or write the owner's papi database.

``scratch_db()`` is a migrated + seeded SQLite file in a temp dir, used through an explicit connection
(KB retrieval, analyst context packs). ``scratch_home()`` points ``PAPI_HOME`` at a temp dir for the
duration (slice pipelines that use the global engine, e.g. the KB fetch/ingest when building the KB
fixture); settings and engine caches are reset on entry and exit.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import Connection

from papi.core.bootstrap import init_home, seed
from papi.core.config import reset_settings
from papi.core.db import get_engine, migrate, reset_engines


@contextmanager
def scratch_db() -> Iterator[Connection]:
    with tempfile.TemporaryDirectory(prefix="papi-eval-db-") as tmp:
        path = Path(tmp) / "eval.db"
        migrate(path)
        engine = get_engine(path)
        try:
            with engine.connect() as conn:
                seed(conn)
                conn.commit()
                yield conn
        finally:
            engine.dispose()


@contextmanager
def scratch_home() -> Iterator[Path]:
    old = os.environ.get("PAPI_HOME")
    with tempfile.TemporaryDirectory(prefix="papi-eval-home-") as tmp:
        os.environ["PAPI_HOME"] = tmp
        reset_settings()
        reset_engines()
        try:
            init_home()
            yield Path(tmp)
        finally:
            reset_engines()
            if old is None:
                os.environ.pop("PAPI_HOME", None)
            else:
                os.environ["PAPI_HOME"] = old
            reset_settings()
