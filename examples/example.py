"""
fastapi-flare — example app (backend: Redis ou SQLite).

Para rodar::

    poetry run uvicorn examples.example:app --reload --port 8001

Rotas disponíveis::

    GET    /                       → health check
    GET    /items/{item_id}        → 404 se id > 100 (WARNING)
    GET    /boom                   → RuntimeError 500 (ERROR)
    GET    /admin                  → 403 Forbidden (WARNING)
    POST   /users                  → cria user — 422 se body inválido, 409 se já existe
    POST   /orders                 → cria pedido — 400/401/404
    POST   /payments               → pagamento — 402/422/500
    DELETE /items/{item_id}        → 404 se não existe (WARNING)
    GET    /flare                  → dashboard de erros
    GET    /flare/metrics          → dashboard de métricas
    GET    /flare/callback         → callback OAuth2 Zitadel (criado automaticamente)
    GET    /docs                   → Scalar API reference
"""
import os

from dotenv import load_dotenv
load_dotenv()

import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from scalar_fastapi import Theme, get_scalar_api_reference
from typing import Optional

from fastapi_flare import FlareConfig, setup

app = FastAPI(title="fastapi-flare example", docs_url=None, redoc_url=None)

# ── FlareConfig ───────────────────────────────────────────────────────────────────
#
# Fluxo ao abrir http://localhost:8001/flare no browser:
#   1. Sem sessão → redireciona para /flare/auth/login
#   2. /flare/auth/login → PKCE challenge → Zitadel
#   3. Login → /flare/callback → sessão criada → /flare liberado ✅
#
# ⚠️  Registre no Zitadel como Redirect URI:
#       http://localhost:8001/flare/callback
#

setup(app, config=FlareConfig(
    # ── Redis ────────────────────────────────────────────────────────────────
    storage_backend="redis",
    redis_host=os.getenv("FLARE_REDIS_HOST", "localhost"),
    redis_port=int(os.getenv("FLARE_REDIS_PORT", "6379")),
    redis_password=os.getenv("FLARE_REDIS_PASSWORD"),
    redis_db=int(os.getenv("FLARE_REDIS_DB", "0")),
    stream_key="flare:logs",
    queue_key="flare:queue",
    max_entries=10_000,
    retention_hours=168,

    # ── Dashboard ────────────────────────────────────────────────────────────
    dashboard_path="/flare",
    dashboard_title="Flare Dashboard",
    dashboard_auth_dependency=None,

    # ── Zitadel — browser PKCE ───────────────────────────────────────────────
    zitadel_domain=os.getenv("FLARE_ZITADEL_DOMAIN"),
    zitadel_client_id=os.getenv("FLARE_ZITADEL_CLIENT_ID"),
    zitadel_project_id=os.getenv("FLARE_ZITADEL_PROJECT_ID"),
    zitadel_redirect_uri=os.getenv("FLARE_ZITADEL_REDIRECT_URI", "http://localhost:8001/flare/callback"),
    zitadel_session_secret=os.getenv("FLARE_ZITADEL_SESSION_SECRET"),

    # ── Worker ───────────────────────────────────────────────────────────────
    worker_interval_seconds=5,
    worker_batch_size=100,
))


# ── Docs ────────────────────────────────────────────────────────────────────────

@app.get("/docs", include_in_schema=False)
async def scalar_docs() -> HTMLResponse:
    return get_scalar_api_reference(
        openapi_url=app.openapi_url,
        title=app.title,
        theme=Theme.DEEP_SPACE,
    )


# ── In-memory fake databases ────────────────────────────────────────────────────

_USERS: dict[int, dict] = {
    1: {"id": 1, "username": "alice", "email": "alice@example.com"},
    2: {"id": 2, "username": "bob",   "email": "bob@example.com"},
}
_ORDERS: dict[int, dict] = {
    100: {"id": 100, "user_id": 1, "product": "Laptop", "total": 1500.00},
}
_PAID_ORDERS: set[int] = set()
_NEXT_USER_ID = 3
_NEXT_ORDER_ID = 101

VALID_COUPONS: set[str] = {"SAVE10", "FLARE20"}
ADMIN_TOKEN = os.getenv("EXAMPLE_ADMIN_TOKEN", "secret-admin-token")  # override via env in prod


# ── Pydantic schemas ─────────────────────────────────────────────────────────────

class UserCreate(BaseModel):
    username: str = Field(..., min_length=3, max_length=32)
    email: str = Field(..., pattern=r"^[^@]+@[^@]+\.[^@]+$")
    age: Optional[int] = Field(None, ge=0, le=150)


class OrderCreate(BaseModel):
    user_id: int
    product: str = Field(..., min_length=1)
    quantity: int = Field(1, ge=1, le=100)
    coupon: Optional[str] = None


class PaymentCreate(BaseModel):
    order_id: int
    amount: float = Field(..., gt=0)
    method: str = Field(..., pattern=r"^(credit|debit|pix)$")


# ── Rotas de teste ──────────────────────────────────────────────────────────────

@app.get("/")
async def root():
    zitadel_domain = os.getenv("FLARE_ZITADEL_DOMAIN")
    zitadel_client_id = os.getenv("FLARE_ZITADEL_CLIENT_ID")
    zitadel_project_id = os.getenv("FLARE_ZITADEL_PROJECT_ID")
    zitadel_redirect_uri = os.getenv("FLARE_ZITADEL_REDIRECT_URI")
    zitadel_on = bool(zitadel_domain and zitadel_client_id and zitadel_project_id)
    auth_mode = (
        "zitadel-browser-pkce" if (zitadel_on and zitadel_redirect_uri)
        else "zitadel-bearer" if zitadel_on
        else "none"
    )
    return {
        "message": "OK",
        "dashboard": "/flare",
        "dashboard_secured": zitadel_on,
        "auth_mode": auth_mode,
        **({"callback": "/flare/callback"} if zitadel_redirect_uri else {}),
    }


@app.get("/items/{item_id}")
async def get_item(item_id: int):
    """Retorna um item. IDs acima de 100 resultam em 404."""
    if item_id > 100:
        raise HTTPException(status_code=404, detail=f"Item {item_id} not found")
    return {"item_id": item_id, "name": f"Item #{item_id}"}


@app.delete("/items/{item_id}")
async def delete_item(item_id: int):
    """Deleta item. IDs acima de 100 não existem → 404."""
    if item_id > 100:
        raise HTTPException(status_code=404, detail=f"Item {item_id} not found — cannot delete")
    return {"deleted": item_id}


@app.get("/admin")
async def admin(x_admin_token: Optional[str] = Header(default=None)):
    """Rota protegida — exige header X-Admin-Token correto → 403 caso contrário."""
    if x_admin_token != ADMIN_TOKEN:
        raise HTTPException(status_code=403, detail="Forbidden — invalid or missing X-Admin-Token")
    return {"message": "Welcome, admin!", "users": len(_USERS), "orders": len(_ORDERS)}


@app.get("/boom")
async def boom():
    """Dispara RuntimeError — testa captura de exceções não tratadas (500 ERROR)."""
    raise RuntimeError("Test exception from /boom")


# ── POST /users ──────────────────────────────────────────────────────────────────

@app.post("/users", status_code=201)
async def create_user(body: UserCreate):
    """
    Cria um novo usuário.
    - 422 se body inválido (Pydantic validation)
    - 409 se username já existe
    """
    global _NEXT_USER_ID
    if any(u["username"] == body.username for u in _USERS.values()):
        raise HTTPException(
            status_code=409,
            detail=f"Username '{body.username}' already exists",
        )
    user = {"id": _NEXT_USER_ID, "username": body.username, "email": body.email, "age": body.age}
    _USERS[_NEXT_USER_ID] = user
    _NEXT_USER_ID += 1
    return user


# ── POST /orders ─────────────────────────────────────────────────────────────────

@app.post("/orders", status_code=201)
async def create_order(
    body: OrderCreate,
    x_auth_token: Optional[str] = Header(default=None),
):
    """
    Cria um pedido.
    - 401 se header X-Auth-Token ausente
    - 404 se user_id não existe
    - 400 se coupon inválido
    - 422 se body inválido (Pydantic)
    """
    global _NEXT_ORDER_ID
    if not x_auth_token:
        raise HTTPException(status_code=401, detail="Missing X-Auth-Token header")
    if body.user_id not in _USERS:
        raise HTTPException(status_code=404, detail=f"User {body.user_id} not found")
    if body.coupon and body.coupon not in VALID_COUPONS:
        raise HTTPException(status_code=400, detail=f"Invalid coupon '{body.coupon}'")
    discount = 0.10 if body.coupon == "SAVE10" else (0.20 if body.coupon == "FLARE20" else 0.0)
    order = {
        "id": _NEXT_ORDER_ID,
        "user_id": body.user_id,
        "product": body.product,
        "quantity": body.quantity,
        "discount": discount,
    }
    _ORDERS[_NEXT_ORDER_ID] = order
    _NEXT_ORDER_ID += 1
    return order


# ── POST /payments ───────────────────────────────────────────────────────────────

@app.post("/payments", status_code=201)
async def create_payment(body: PaymentCreate):
    """
    Processa pagamento.
    - 422 se body inválido (Pydantic)
    - 404 se order_id não existe
    - 409 se pedido já foi pago
    - 402 se amount não corresponde ao total do pedido  (simulado: total > amount * 1.5)
    - 500 se amount == 13.37 (trigger de bug simulado)
    """
    if body.order_id not in _ORDERS:
        raise HTTPException(status_code=404, detail=f"Order {body.order_id} not found")
    if body.order_id in _PAID_ORDERS:
        raise HTTPException(status_code=409, detail=f"Order {body.order_id} already paid")
    if body.amount == 13.37:  # simula bug de produção
        raise RuntimeError(f"Billing engine fault — amount={body.amount} triggered a known bug")
    order = _ORDERS[body.order_id]
    expected = order.get("total", body.amount)  # some orders lack total
    if isinstance(expected, float) and body.amount < expected * 0.5:
        raise HTTPException(
            status_code=402,
            detail=f"Insufficient amount {body.amount} for order total {expected}",
        )
    _PAID_ORDERS.add(body.order_id)
    return {"paid": True, "order_id": body.order_id, "method": body.method, "charged": body.amount}


if __name__ == "__main__":
    uvicorn.run("examples.example:app", host="0.0.0.0", port=8000, reload=True)
