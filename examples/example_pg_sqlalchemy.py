"""
fastapi-flare — PostgreSQL + SQLAlchemy async ORM example.

Demonstra a integração completa com:
  - Backend fastapi-flare usando PostgreSQL (banco: teste_db)
  - SQLAlchemy async ORM (asyncpg driver)
  - setup_sqlalchemy(engine) para correlacionar queries ao request_id do Flare

Para rodar::

    poetry run uvicorn examples.example_pg_sqlalchemy:app --reload --port 8001

Rotas::

    GET    /                       → health check
    GET    /products               → lista produtos (SELECT)
    POST   /products               → cria produto (INSERT)
    GET    /products/{id}          → busca produto (SELECT — 404 se não existe)
    DELETE /products/{id}          → remove produto (DELETE — 404 se não existe)
    GET    /boom                   → RuntimeError 500
    GET    /flare                  → dashboard de erros
    GET    /flare/requests         → dashboard de requests
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import AsyncGenerator, Optional

import uvicorn
from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field
from dotenv import load_dotenv

# SQLAlchemy async
from sqlalchemy import BigInteger, Float, String, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# fastapi-flare
from fastapi_flare import FlareConfig, setup, setup_sqlalchemy

load_dotenv()

# ── Database connection ────────────────────────────────────────────────────────
# Uses teste_db — deve existir no servidor PostgreSQL.
PG_DSN = os.getenv(
    "EXAMPLE_PG_DSN",
    "postgresql+asyncpg://Flare:%40Bardo8389406@217.216.81.128:5432/teste_db",
)

engine = create_async_engine(PG_DSN, echo=False, pool_size=5, max_overflow=10)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


# ── ORM model ──────────────────────────────────────────────────────────────────

class Base(DeclarativeBase):
    pass


class Product(Base):
    __tablename__ = "products"

    id:    Mapped[int]   = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    name:  Mapped[str]   = mapped_column(String(120), nullable=False)
    price: Mapped[float] = mapped_column(Float, nullable=False)
    stock: Mapped[int]   = mapped_column(BigInteger, default=0, nullable=False)


# ── Create tables on startup ───────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    await engine.dispose()


# ── App & Flare setup ──────────────────────────────────────────────────────────

app = FastAPI(title="Flare PG SQLAlchemy example", lifespan=lifespan)

flare = setup(app, config=FlareConfig(
    storage_backend="postgresql",
    pg_dsn=os.getenv("FLARE_PG_DSN"),
    pg_table_name="flare_logs_teste",   # isolado do banco principal
    dashboard_path="/flare",
    dashboard_title="Flare — PG SQLAlchemy",
    track_requests=True,
    track_2xx_requests=True,             # captura todos os status neste exemplo
    capture_request_headers=False,
    worker_interval_seconds=5,
))

# Registra os event listeners do SQLAlchemy — queries passam a ter request_id
setup_sqlalchemy(engine)


# ── Dependency ─────────────────────────────────────────────────────────────────

async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with SessionLocal() as session:
        yield session


# ── Pydantic schemas ───────────────────────────────────────────────────────────

class ProductCreate(BaseModel):
    name:  str   = Field(..., min_length=1, max_length=120)
    price: float = Field(..., gt=0)
    stock: int   = Field(0, ge=0)


class ProductOut(BaseModel):
    id:    int
    name:  str
    price: float
    stock: int

    model_config = {"from_attributes": True}


# ── Routes ─────────────────────────────────────────────────────────────────────

@app.get("/")
async def root():
    return {
        "message": "fastapi-flare + PostgreSQL + SQLAlchemy",
        "dashboard": "/flare",
        "requests_tab": "/flare/requests",
        "docs": "/docs",
    }


@app.get("/products", response_model=list[ProductOut])
async def list_products(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Product).order_by(Product.id))
    return result.scalars().all()


@app.post("/products", response_model=ProductOut, status_code=201)
async def create_product(
    body: ProductCreate,
    db: AsyncSession = Depends(get_db),
):
    product = Product(name=body.name, price=body.price, stock=body.stock)
    db.add(product)
    await db.commit()
    await db.refresh(product)
    return product


@app.get("/products/{product_id}", response_model=ProductOut)
async def get_product(
    product_id: int,
    db: AsyncSession = Depends(get_db),
):
    row = await db.get(Product, product_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Product {product_id} not found")
    return row


@app.delete("/products/{product_id}", status_code=204)
async def delete_product(
    product_id: int,
    db: AsyncSession = Depends(get_db),
):
    row = await db.get(Product, product_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Product {product_id} not found")
    await db.delete(row)
    await db.commit()


@app.get("/boom")
async def boom():
    """Trigger 500 para testar captura de erros."""
    raise RuntimeError("Boom! Test exception from example_pg_sqlalchemy")


@app.get("/db-check")
async def db_check(db: AsyncSession = Depends(get_db)):
    """Executa SELECT 1 no banco real via SQLAlchemy."""
    result = await db.execute(text("SELECT version()"))
    version = result.scalar()
    return {"db": "postgresql", "version": version}


if __name__ == "__main__":
    uvicorn.run("examples.example_pg_sqlalchemy:app", host="0.0.0.0", port=8001, reload=True)
