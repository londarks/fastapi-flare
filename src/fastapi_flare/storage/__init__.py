"""
Storage package for fastapi-flare.
=====================================

Public surface::

    from fastapi_flare.storage import make_storage, FlareStorageProtocol

:func:`make_storage` is the single entry point for instantiating a backend.
The rest of the application — handlers, worker, router — only speak to
:class:`FlareStorageProtocol` and never import concrete backends directly.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi_flare.storage.base import FlareStorageProtocol

if TYPE_CHECKING:
    from fastapi_flare.config import FlareConfig

__all__ = ["make_storage", "FlareStorageProtocol"]


def make_storage(config: "FlareConfig") -> FlareStorageProtocol:
    """
    Instantiate the storage backend declared in ``config.storage_backend``.

    Supported values:

    * ``"sqlite"`` (default)  — :class:`~fastapi_flare.storage.sqlite_storage.SQLiteStorage`
      Zero-config local file, ideal for development and quick testing.
    * ``"postgresql"``        — :class:`~fastapi_flare.storage.pg_storage.PostgreSQLStorage`
      Production-grade backend, requires a running PostgreSQL instance.

    Args:
        config: The resolved :class:`~fastapi_flare.config.FlareConfig`.

    Returns:
        A concrete implementation of :class:`FlareStorageProtocol`.

    Raises:
        ValueError: If ``storage_backend`` is not a known value.
    """
    backend = config.storage_backend

    if backend == "sqlite":
        from fastapi_flare.storage.sqlite_storage import SQLiteStorage
        return SQLiteStorage(config)

    if backend == "postgresql":
        from fastapi_flare.storage.pg_storage import PostgreSQLStorage
        return PostgreSQLStorage(config)

    raise ValueError(
        f"Unknown storage_backend: '{backend}'. "
        "Supported values: 'postgresql', 'sqlite'."
    )
