"""
fastapi-flare — example app.

Demonstra duas conexões: sem auth e com Zitadel.
Configure as variáveis no .env (copie de .env.example).
O FlareConfig lê automaticamente as variáveis FLARE_* do .env.

Para rodar sem Zitadel (modo básico)::

    poetry run uvicorn examples.example:app --reload --port 8000

Para rodar com Zitadel (dashboard protegido)::

    # 1. Instale os extras de auth:
    #    pip install "fastapi-flare[auth]"
    #
    # 2. Crie um .env com as variáveis Zitadel:
    #    FLARE_ZITADEL_DOMAIN=auth.mycompany.com
    #    FLARE_ZITADEL_CLIENT_ID=<your-client-id>
    #    FLARE_ZITADEL_PROJECT_ID=<your-project-id>
    #
    # 3. Rode normalmente:
    #    poetry run uvicorn examples.example:app --reload --port 8000

Rotas de teste::

    GET  /            → health check
    GET  /boom        → dispara RuntimeError  (ERROR no dashboard)
    GET  /items/999   → dispara HTTPException 404 (WARNING no dashboard)
    GET  /flare       → dashboard de erros (protegido se Zitadel estiver configurado)
    GET  /docs        → Scalar API reference
"""
import uvicorn
from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from scalar_fastapi import Theme, get_scalar_api_reference

from fastapi_flare import FlareConfig, setup

app = FastAPI(title="fastapi-flare example", docs_url=None, redoc_url=None)

# ── FlareConfig ───────────────────────────────────────────────────────────────────
#
# FlareConfig lê as variáveis FLARE_* automaticamente do .env.
#
# Modo básico (sem auth): apenas FLARE_REDIS_* necessárias.
#
# Modo Zitadel (dashboard protegido): adicione ao .env:
#   FLARE_ZITADEL_DOMAIN=auth.mycompany.com
#   FLARE_ZITADEL_CLIENT_ID=<your-client-id>
#   FLARE_ZITADEL_PROJECT_ID=<your-project-id>
#
# Quando os três campos Zitadel estiverem presentes, setup() injeta
# automaticamente o Depends de validação JWT no dashboard /flare.
# Não é necessário nenhum código adicional.
#
config = FlareConfig()
setup(app, config=config)


# ── Docs ────────────────────────────────────────────────────────────────────────

@app.get("/docs", include_in_schema=False)
async def scalar_docs() -> HTMLResponse:
    return get_scalar_api_reference(
        openapi_url=app.openapi_url,
        title=app.title,
        theme=Theme.DEEP_SPACE,
    )


# ── Rotas de teste ──────────────────────────────────────────────────────────────

@app.get("/")
async def root():
    secured = bool(
        config.zitadel_domain
        and config.zitadel_client_id
        and config.zitadel_project_id
    )
    return {
        "message": "OK",
        "dashboard": "/flare",
        "dashboard_secured": secured,
        "auth": "zitadel" if secured else "none",
    }


@app.get("/items/{item_id}")
async def get_item(item_id: int):
    """Retorna um item. IDs acima de 100 resultam em 404 (WARNING no dashboard)."""
    if item_id > 100:
        raise HTTPException(status_code=404, detail=f"Item {item_id} not found")
    return {"item_id": item_id, "name": f"Item #{item_id}"}


@app.get("/boom")
async def boom():
    """Dispara RuntimeError para testar captura de erros não tratados (ERROR no dashboard)."""
    raise RuntimeError("Test exception from /boom")


if __name__ == "__main__":
    uvicorn.run("examples.example:app", host="0.0.0.0", port=8000, reload=True)
