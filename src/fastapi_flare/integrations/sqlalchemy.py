"""
SQLAlchemy integration for fastapi-flare.

Attaches SQLAlchemy Core/ORM engine event listeners that propagate the
current ``request_id`` (set by :class:`~fastapi_flare.middleware.RequestIdMiddleware`)
into every database query executed within the same async task context.

Usage::

    from sqlalchemy.ext.asyncio import create_async_engine
    from fastapi_flare import setup
    from fastapi_flare.integrations.sqlalchemy import setup_sqlalchemy

    engine = create_async_engine("postgresql+asyncpg://...")

    app = FastAPI()
    flare_config = setup(app)
    setup_sqlalchemy(engine)

After calling ``setup_sqlalchemy``, Flare can correlate slow/failing
queries to the originating HTTP request via ``request_id``.

.. note::
    This module has zero hard dependencies on SQLAlchemy — it imports
    ``sqlalchemy.event`` only inside :func:`setup_sqlalchemy` so that the
    rest of fastapi-flare works without SQLAlchemy installed.
"""
from __future__ import annotations

import time
from contextvars import ContextVar
from typing import Any

# Per-request accumulated query list: list of {"sql": ..., "duration_ms": ..., "request_id": ...}
_flare_query_log_var: ContextVar[list[dict] | None] = ContextVar(
    "flare_query_log", default=None
)


def setup_sqlalchemy(engine: Any) -> None:
    """
    Register ``before_cursor_execute`` / ``after_cursor_execute`` listeners
    on *engine* to track query durations per HTTP request.

    :param engine: A SQLAlchemy ``Engine`` or ``AsyncEngine`` instance.

    The listeners are lightweight:
      - ``before``: stores ``time.perf_counter()`` on the connection's
        execution context.
      - ``after``: measures the elapsed time and appends a small dict to the
        per-request ``ContextVar`` list.

    The per-request query log is accessible via
    :func:`get_current_request_queries` from any code running within the
    same async task (e.g. an exception handler or a custom middleware).
    """
    try:
        from sqlalchemy import event  # type: ignore[import]
    except ImportError as exc:
        raise ImportError(
            "SQLAlchemy is required to use setup_sqlalchemy. "
            "Install it with: pip install sqlalchemy"
        ) from exc

    # AsyncEngine wraps a sync engine; unwrap if needed.
    sync_engine = getattr(engine, "sync_engine", engine)

    from fastapi_flare.middleware import _flare_request_id_var

    @event.listens_for(sync_engine, "before_cursor_execute")
    def _before(
        conn: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        if context is not None:
            context._flare_t0 = time.perf_counter()

    @event.listens_for(sync_engine, "after_cursor_execute")
    def _after(
        conn: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        if context is None:
            return

        t0: float | None = getattr(context, "_flare_t0", None)
        duration_ms = int((time.perf_counter() - t0) * 1000) if t0 is not None else None

        request_id = _flare_request_id_var.get()

        # Append to the per-request query log.
        log = _flare_query_log_var.get()
        if log is None:
            log = []
            _flare_query_log_var.set(log)
        log.append(
            {
                "sql": statement,
                "duration_ms": duration_ms,
                "request_id": request_id,
            }
        )


def get_current_request_queries() -> list[dict]:
    """
    Return the list of SQL queries executed so far in the current async task.

    Each entry is a dict with keys:
      - ``sql`` (str): the SQL statement
      - ``duration_ms`` (int | None): execution time in milliseconds
      - ``request_id`` (str | None): the Flare request id, if available

    Returns an empty list when called outside a request context or before
    any queries have been executed.
    """
    return _flare_query_log_var.get() or []
