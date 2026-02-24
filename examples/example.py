"""
fastapi-flare — example app.

Configure o Redis no .env (veja .env.example).
O FlareConfig lê automaticamente as variáveis FLARE_* do .env.

Run:
    poetry run uvicorn examples.example:app --reload --port 8000

Rotas de teste:
    GET /           -> OK
    GET /boom       -> dispara RuntimeError (ERROR no dashboard)
    GET /items/999  -> dispara HTTPException 404 (WARNING no dashboard)
    GET /flare      -> dashboard de erros
"""
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from scalar_fastapi import get_scalar_api_reference
from scalar_fastapi import Theme

from fastapi_flare import FlareConfig, setup

app = FastAPI(title="fastapi-flare example", docs_url=None, redoc_url=None)

# FlareConfig carrega automaticamente as variáveis FLARE_* do .env
setup(app, config=FlareConfig())


@app.get("/docs", include_in_schema=False)
async def scalar_docs() -> HTMLResponse:
    return get_scalar_api_reference(
        openapi_url=app.openapi_url,
        title=app.title,
        theme=Theme.DEEP_SPACE,
    )


@app.get("/")
async def root():
    return {"message": "OK", "dashboard": "/flare"}


@app.get("/items/{item_id}")
async def get_item(item_id: int):
    if item_id > 100:
        raise HTTPException(status_code=404, detail=f"Item {item_id} not found")
    return {"item_id": item_id, "name": f"Item #{item_id}"}


@app.get("/boom")
async def boom():
    """Dispara um RuntimeError para testar a captura de ERROR."""
    raise RuntimeError("Test exception from /boom")


if __name__ == "__main__":
    uvicorn.run("examples.example:app", host="0.0.0.0", port=8000, reload=True)
