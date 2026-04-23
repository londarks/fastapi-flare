"""
Python logging integration for fastapi-flare.
=============================================

Captures non-HTTP errors into the Flare dashboard by attaching a
:class:`logging.Handler` that forwards every ``WARNING``/``ERROR`` record
to :func:`fastapi_flare.queue.push_log`.

Typical uses:
  - Errors raised inside background tasks, workers, cron jobs.
  - ``logger.exception(...)`` calls anywhere in user code.
  - Exceptions from ``asyncio.create_task`` that would otherwise be
    silently swallowed by the event loop.

Design invariants
-----------------
- ``FlareLogHandler.emit()`` MUST NEVER raise — logging faults cannot
  break the caller's code path.
- When called from inside an asyncio loop, the write is scheduled as a
  fire-and-forget task (non-blocking).
- When called from a sync context with no running loop (e.g. app startup,
  threads, ``logging.shutdown()``), the write is dispatched to a private
  background thread so the caller never blocks.

Usage::

    # Auto-installed when FlareConfig.capture_logging = True.
    # Manual install:
    from fastapi_flare.integrations.logging import install_logging_capture
    install_logging_capture(config)

    logger = logging.getLogger("myapp.worker")
    logger.exception("payment job failed")  # appears in /flare
"""
from __future__ import annotations

import asyncio
import logging
import threading
import traceback
from typing import TYPE_CHECKING, Iterable, Optional

if TYPE_CHECKING:
    from fastapi_flare.config import FlareConfig


_HANDLER_ATTR = "_flare_logging_handler"


class FlareLogHandler(logging.Handler):
    """
    ``logging.Handler`` that forwards WARNING+ records to Flare storage.

    Installed once per ``FlareConfig`` via :func:`install_logging_capture`.
    Safe against re-entrancy: records emitted while writing to storage are
    ignored via a thread-local flag.
    """

    _local = threading.local()

    def __init__(self, config: "FlareConfig", *, level: int = logging.WARNING) -> None:
        super().__init__(level=level)
        self.config = config

    def emit(self, record: logging.LogRecord) -> None:  # noqa: D401
        # Guard against infinite recursion: if push_log itself logs something
        # at WARNING+, we must not re-enter this handler.
        if getattr(self._local, "in_emit", False):
            return
        self._local.in_emit = True
        try:
            self._dispatch(record)
        except Exception:  # noqa: BLE001
            # Logging must never break the caller.
            pass
        finally:
            self._local.in_emit = False

    def _dispatch(self, record: logging.LogRecord) -> None:
        from fastapi_flare.queue import push_log

        level = "ERROR" if record.levelno >= logging.ERROR else "WARNING"

        stack_trace: Optional[str] = None
        error: Optional[str] = None
        if record.exc_info and record.exc_info[0] is not None:
            exc_type, exc_val, exc_tb = record.exc_info
            stack_trace = "".join(traceback.format_exception(exc_type, exc_val, exc_tb))
            error = f"{exc_type.__name__}: {exc_val}"

        context = {
            "logger": record.name,
            "module": record.module,
            "func": record.funcName,
            "line": record.lineno,
        }
        if record.pathname:
            context["file"] = record.pathname

        coro = push_log(
            self.config,
            level=level,
            event=f"log.{record.name}",
            message=record.getMessage(),
            error=error,
            stack_trace=stack_trace,
            context=context,
        )

        _schedule(coro)


def _schedule(coro) -> None:
    """Run *coro* without blocking the caller.

    - Inside a running asyncio loop: schedule as a background task.
    - Outside: dispatch to a private background thread that owns its own loop.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop is not None:
        try:
            loop.create_task(coro)
            return
        except Exception:  # noqa: BLE001
            pass  # fall through to thread fallback

    # No running loop — run on the private worker thread.
    _BackgroundLoop.submit(coro)


class _BackgroundLoop:
    """
    Lazily-started daemon thread hosting an asyncio event loop.

    Used as a sink for log records emitted outside any running loop
    (startup code, plain threads, shutdown handlers).
    """

    _loop: Optional[asyncio.AbstractEventLoop] = None
    _thread: Optional[threading.Thread] = None
    _lock = threading.Lock()

    @classmethod
    def submit(cls, coro) -> None:
        loop = cls._ensure_loop()
        try:
            asyncio.run_coroutine_threadsafe(coro, loop)
        except Exception:  # noqa: BLE001
            # Close the coroutine to avoid "coroutine was never awaited" warnings.
            try:
                coro.close()
            except Exception:  # noqa: BLE001
                pass

    @classmethod
    def _ensure_loop(cls) -> asyncio.AbstractEventLoop:
        with cls._lock:
            if cls._loop is not None and cls._loop.is_running():
                return cls._loop
            cls._loop = asyncio.new_event_loop()
            cls._thread = threading.Thread(
                target=cls._run,
                args=(cls._loop,),
                name="flare-logging-loop",
                daemon=True,
            )
            cls._thread.start()
            return cls._loop

    @staticmethod
    def _run(loop: asyncio.AbstractEventLoop) -> None:
        asyncio.set_event_loop(loop)
        try:
            loop.run_forever()
        finally:
            loop.close()


def install_logging_capture(
    config: "FlareConfig",
    *,
    loggers: Optional[Iterable[str]] = None,
    level: int = logging.WARNING,
) -> FlareLogHandler:
    """
    Attach a :class:`FlareLogHandler` to the target loggers.

    :param config:   Active FlareConfig (after ``setup()``).
    :param loggers:  Iterable of logger names. ``None`` attaches to the
                     root logger, which captures records from every logger
                     that propagates.
    :param level:    Minimum level to forward (default: WARNING).
    :returns:        The installed handler (for later removal if desired).

    Idempotent: calling twice for the same config replaces the previous
    handler instead of adding a duplicate.
    """
    handler = FlareLogHandler(config, level=level)

    # Remove any previously-installed handler tied to this config.
    previous: Optional[FlareLogHandler] = getattr(config, _HANDLER_ATTR, None)
    if previous is not None:
        uninstall_logging_capture(config)

    targets: list[logging.Logger]
    if loggers is None:
        targets = [logging.getLogger()]  # root
    else:
        targets = [logging.getLogger(n) for n in loggers]

    for lg in targets:
        lg.addHandler(handler)
        if lg.level == logging.NOTSET or lg.level > level:
            lg.setLevel(level)

    setattr(config, _HANDLER_ATTR, handler)
    setattr(config, _HANDLER_ATTR + "_targets", targets)
    return handler


def uninstall_logging_capture(config: "FlareConfig") -> None:
    """Detach the handler previously installed for *config* (if any)."""
    handler: Optional[FlareLogHandler] = getattr(config, _HANDLER_ATTR, None)
    if handler is None:
        return
    targets: list[logging.Logger] = getattr(config, _HANDLER_ATTR + "_targets", [])
    for lg in targets:
        try:
            lg.removeHandler(handler)
        except Exception:  # noqa: BLE001
            pass
    try:
        delattr(config, _HANDLER_ATTR)
        delattr(config, _HANDLER_ATTR + "_targets")
    except AttributeError:
        pass


def install_asyncio_capture(config: "FlareConfig") -> None:
    """
    Install an asyncio loop exception handler that forwards unhandled
    task errors to Flare.

    Must be called from inside a running event loop (typically the
    lifespan startup phase). When no loop is active, this is a no-op.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return

    previous = loop.get_exception_handler()

    def _handler(_loop: asyncio.AbstractEventLoop, context: dict) -> None:
        try:
            _forward_asyncio_context(config, context)
        finally:
            if previous is not None:
                try:
                    previous(_loop, context)
                except Exception:  # noqa: BLE001
                    pass
            else:
                _loop.default_exception_handler(context)

    loop.set_exception_handler(_handler)


def _forward_asyncio_context(config: "FlareConfig", context: dict) -> None:
    from fastapi_flare.queue import push_log

    exc: Optional[BaseException] = context.get("exception")
    message = context.get("message") or (str(exc) if exc else "asyncio error")

    stack_trace: Optional[str] = None
    error: Optional[str] = None
    if exc is not None:
        stack_trace = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        error = f"{type(exc).__name__}: {exc}"

    safe_context = {k: repr(v)[:500] for k, v in context.items() if k != "exception"}

    coro = push_log(
        config,
        level="ERROR",
        event="asyncio.unhandled",
        message=message,
        error=error,
        stack_trace=stack_trace,
        context=safe_context,
    )
    _schedule(coro)
