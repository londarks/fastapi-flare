"""
fastapi-flare — exemplo de captura de erros NÃO-HTTP.

Mostra os 3 mecanismos novos:

  1. ``logger.exception(...)`` em qualquer lugar do código → vira log via
     FlareLogHandler (configurado por ``capture_logging=True``).
  2. ``capture_exception(e, ...)`` — API manual para registrar um erro
     que você já tratou, sem precisar levantar de novo.
  3. ``asyncio.create_task(...)`` que explode — capturado pelo loop
     exception handler (``capture_asyncio_errors=True``).

Como rodar::

    poetry run uvicorn examples.example_non_http_capture:app --reload --port 8002

Rotas de teste::

    GET  /                    → health
    GET  /trigger/logger      → dispara logger.exception no handler
    GET  /trigger/manual      → chama capture_exception() manualmente
    GET  /trigger/asyncio     → cria task fire-and-forget que explode
    GET  /trigger/background  → agenda trabalho em thread externa que falha
    GET  /trigger/warn        → capture_message() nível WARNING
    GET  /flare               → dashboard — todas as entradas aparecem aqui,
                                mesmo sem endpoint associado.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time

from fastapi import FastAPI

from fastapi_flare import (
    FlareConfig,
    capture_exception,
    capture_message,
    setup,
)


# ── Logger do app do usuário ────────────────────────────────────────────────
# Qualquer chamada a logger.exception / logger.error vai aparecer no Flare
# porque capture_logging=True instala um handler na raiz.
logger = logging.getLogger("myapp.worker")
logging.basicConfig(level=logging.INFO)


app = FastAPI(title="fastapi-flare — non-HTTP capture demo")

config = setup(
    app,
    config=FlareConfig(
        storage_backend="sqlite",
        sqlite_path="flare_non_http.db",
        dashboard_path="/flare",
        dashboard_title="Flare — Non-HTTP Capture",
        capture_logging=True,
        capture_asyncio_errors=True,
        # Força Zitadel OFF para esse exemplo, ignorando FLARE_ZITADEL_*
        # que possam estar no .env — queremos /flare aberto para teste local.
        zitadel_domain=None,
        zitadel_client_id=None,
        zitadel_project_id=None,
        zitadel_redirect_uri=None,
        zitadel_session_secret=None,
    ),
)


# ── Rotas de teste ──────────────────────────────────────────────────────────

@app.get("/")
async def index() -> dict:
    return {
        "ok": True,
        "try": [
            "/trigger/logger",
            "/trigger/manual",
            "/trigger/asyncio",
            "/trigger/background",
            "/trigger/warn",
        ],
        "dashboard": "/flare",
    }


@app.get("/trigger/logger")
async def trigger_logger() -> dict:
    """Erro capturado pelo logging.Handler — aparece com event=log.myapp.worker."""
    try:
        payload = {"amount": "not-a-number"}
        _ = int(payload["amount"])
    except ValueError:
        logger.exception("failed to process payment payload")
    return {"ok": True, "captured_via": "logging.Handler"}


@app.get("/trigger/manual")
async def trigger_manual() -> dict:
    """Erro tratado explicitamente via capture_exception()."""
    try:
        raise RuntimeError("third-party webhook returned 502")
    except RuntimeError as e:
        await capture_exception(
            e,
            event="webhook.retry_exhausted",
            context={"provider": "stripe", "attempts": 5},
        )
    return {"ok": True, "captured_via": "capture_exception"}


@app.get("/trigger/asyncio")
async def trigger_asyncio() -> dict:
    """Task fire-and-forget que explode — capturada pelo loop handler."""
    async def doomed() -> None:
        await asyncio.sleep(0.05)
        raise ZeroDivisionError("background task blew up")

    asyncio.create_task(doomed())
    return {"ok": True, "captured_via": "asyncio.set_exception_handler"}


@app.get("/trigger/background")
async def trigger_background() -> dict:
    """Erro em uma thread externa — pego pelo logging.exception dentro dela."""
    def worker() -> None:
        time.sleep(0.05)
        try:
            raise ConnectionError("lost connection to queue broker")
        except ConnectionError:
            logger.exception("broker thread failed")

    threading.Thread(target=worker, daemon=True).start()
    return {"ok": True, "captured_via": "thread + logging.Handler"}


@app.get("/trigger/warn")
async def trigger_warn() -> dict:
    """Mensagem livre nível WARNING — sem exceção, só sinal de alerta."""
    await capture_message(
        "rate-limit hit on outbound API",
        level="WARNING",
        event="outbound.rate_limited",
        context={"api": "sendgrid", "window_seconds": 60, "hits": 142},
    )
    return {"ok": True, "captured_via": "capture_message"}


# ── Trabalho periódico que NÃO é request HTTP ───────────────────────────────
# Roda uma vez no startup pra provar que logs fora do request path aparecem
# imediatamente no dashboard — sem ninguém bater em nenhuma rota.
@app.on_event("startup")
async def emit_startup_sample() -> None:
    logger.warning("app booted in degraded mode: cache_disabled=True")
    await capture_message(
        "deploy finished",
        level="WARNING",
        event="deploy.completed",
        context={"version": "1.4.2", "commit": "abc1234"},
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "examples.example_non_http_capture:app",
        host="0.0.0.0",
        port=8002,
        reload=True,
    )
