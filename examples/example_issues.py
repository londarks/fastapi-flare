"""
fastapi-flare — exemplo do agrupamento de erros (Issues).

Este exemplo existe para exercitar a aba **Issues** (v0.3.0): toda vez que
um erro é capturado, o Flare calcula um fingerprint determinístico
``(exception_type, endpoint, top-5 frames normalizadas)`` e faz upsert numa
tabela de issues. 500 ocorrências do mesmo bug viram **1 issue** com
``occurrence_count = 500``, não 500 linhas isoladas.

Como rodar::

    poetry run uvicorn examples.example_issues:app --reload --port 8003

Depois abra http://localhost:8003/flare/issues e dispare tráfego nas rotas
abaixo — de preferência várias vezes cada uma, pra ver os contadores subindo
ao vivo.

Rotas de teste::

    GET  /                         → health + atalhos
    GET  /boom/value-error         → mesmo ValueError sempre (1 issue)
    GET  /boom/key-error           → mesmo KeyError sempre (1 issue)
    GET  /boom/deep                → ValueError vindo de uma função diferente
                                      (mesma exception, stack diferente → issue nova)
    GET  /items/{iid}              → 404 HTTPException
    GET  /users                    → 403 HTTPException
    POST /signup                   → 422 validation error (corpo vazio/errado)
    POST /orders                   → 500 se o payload mentir sobre o preço
    GET  /trigger/manual           → capture_exception(...) fora do request path

    GET  /stress/:n                → gera N erros aleatórios de uma vez para
                                      popular o dashboard rapidamente

Cenários interessantes para validar na aba Issues::

    1. Chame /boom/value-error 10× → 1 issue, occurrence_count=10
    2. Chame /boom/key-error  5×  → issue nova (tipo de exception diferente)
    3. Chame /boom/deep  3×       → issue nova (mesmo ValueError, stack diferente)
    4. Marque uma issue como "Resolved" no dashboard → ela some do filtro "Open"
    5. Chame /boom/value-error de novo → a issue Resolved é **reaberta
       automaticamente** com count +1
    6. Chame /stress/50 → poluição controlada para exercitar paginação
"""
from __future__ import annotations

import random

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from fastapi_flare import FlareConfig, capture_exception, setup


app = FastAPI(title="fastapi-flare — Issues demo")

config = setup(
    app,
    config=FlareConfig(
        storage_backend="sqlite",
        sqlite_path="flare_issues_demo.db",
        dashboard_path="/flare",
        dashboard_title="Flare — Issues Demo",
        # Força auth OFF, ignorando FLARE_ZITADEL_* / dashboard_auth_dependency
        # que possam vir do .env. Mantém /flare aberto para teste local.
        zitadel_domain=None,
        zitadel_client_id=None,
        zitadel_project_id=None,
        zitadel_redirect_uri=None,
        zitadel_session_secret=None,
    ),
)


# ── Modelos ─────────────────────────────────────────────────────────────────

class SignupBody(BaseModel):
    email: str = Field(..., min_length=5)
    password: str = Field(..., min_length=8)


class OrderBody(BaseModel):
    item_id: int
    quantity: int = Field(..., gt=0)
    unit_price_cents: int = Field(..., gt=0)


# ── Helpers que moram em funções diferentes (stack trace distinto) ──────────

def _parse_positive_int(raw: str) -> int:
    # Essa função aparece no topo do stacktrace dos /boom/value-error.
    value = int(raw)
    if value <= 0:
        raise ValueError(f"must be positive, got {value}")
    return value


def _deep_calculation(raw: str) -> int:
    # Mesma exception (ValueError), outra função → fingerprint diferente.
    value = raw.strip()
    return _do_math(value)


def _do_math(value: str) -> int:
    # ValueError nasce aqui dentro. Top da stack será _do_math, não _parse_positive_int.
    return int(value) * 2


# ── Rotas HTTP ──────────────────────────────────────────────────────────────

@app.get("/")
async def index() -> dict:
    return {
        "ok": True,
        "dashboard": "/flare/issues",
        "errors_stream": "/flare",
        "try": [
            "/boom/value-error",
            "/boom/key-error",
            "/boom/deep",
            "/items/42",
            "/items/999",
            "/users",
            "POST /signup   {\"email\":\"x\",\"password\":\"y\"}",
            "POST /orders   {\"item_id\":1,\"quantity\":1,\"unit_price_cents\":0}",
            "/trigger/manual",
            "/stress/25",
        ],
    }


@app.get("/boom/value-error")
async def boom_value_error() -> dict:
    """Sempre levanta o MESMO ValueError vindo da mesma função → 1 issue."""
    _parse_positive_int("-1")
    return {"never": "reached"}


@app.get("/boom/key-error")
async def boom_key_error() -> dict:
    """Sempre levanta o MESMO KeyError → issue própria (tipo de exception)."""
    data: dict[str, int] = {"a": 1}
    return {"value": data["missing_key"]}


@app.get("/boom/deep")
async def boom_deep() -> dict:
    """Mesmo ValueError, stacktrace diferente → issue separada do /boom/value-error."""
    return {"result": _deep_calculation("not-a-number")}


@app.get("/items/{iid}")
async def read_item(iid: int) -> dict:
    if iid == 42:
        return {"id": 42, "name": "The Answer"}
    raise HTTPException(status_code=404, detail=f"item {iid} not found")


@app.get("/users")
async def list_users() -> dict:
    # 403 do mesmo endpoint → 1 issue
    raise HTTPException(status_code=403, detail="forbidden: admin only")


@app.post("/signup")
async def signup(body: SignupBody) -> dict:
    """Corpos inválidos geram 422 — todos agrupados numa issue de /signup."""
    return {"ok": True, "email": body.email}


@app.post("/orders")
async def create_order(body: OrderBody) -> dict:
    """500 manual quando o preço bate em zero depois do cálculo."""
    total = body.quantity * body.unit_price_cents
    if total < 100:
        raise RuntimeError(f"suspicious order total: {total} cents")
    return {"ok": True, "total_cents": total}


@app.get("/trigger/manual")
async def trigger_manual() -> dict:
    """Erro capturado FORA do request path via capture_exception()."""
    try:
        raise ConnectionError("outbound webhook timed out")
    except ConnectionError as e:
        await capture_exception(
            e,
            event="webhook.timeout",
            context={"provider": "stripe", "attempts": 3},
        )
    return {"ok": True, "captured_via": "capture_exception"}


# ── Gerador de ruído para testar paginação + agrupamento em volume ──────────

@app.get("/stress/{n}")
async def stress(n: int) -> dict:
    """
    Dispara ``n`` erros internamente — misturando 3 tipos em proporções
    distintas para provar que o dashboard colapsa corretamente:
      - ~60% ValueError de /boom/value-error
      - ~30% KeyError de /boom/key-error
      - ~10% RuntimeError inédito (entra como issue nova)
    """
    n = max(1, min(n, 500))
    rng = random.Random(42)
    for _ in range(n):
        roll = rng.random()
        try:
            if roll < 0.6:
                _parse_positive_int("-1")
            elif roll < 0.9:
                data: dict[str, int] = {"a": 1}
                _ = data["missing_key"]
            else:
                raise RuntimeError("stress-generated surprise")
        except Exception as e:
            await capture_exception(e, event="stress.generated")
    return {"ok": True, "generated": n}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "examples.example_issues:app",
        host="0.0.0.0",
        port=8003,
        reload=True,
    )
