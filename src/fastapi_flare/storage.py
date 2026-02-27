"""
fastapi-flare storage — compatibility shim.
=============================================

.. deprecated::
    This module has been superseded by the ``fastapi_flare.storage`` package
    (``storage/__init__.py``, ``storage/base.py``, ``storage/sqlite_storage.py``,
    ``storage/pg_storage.py``).

    The public surface is now:

    .. code-block:: python

        from fastapi_flare.storage import make_storage, FlareStorageProtocol

    Internal callers (worker, router) no longer import from this file —
    they interact exclusively through ``config.storage_instance``.
    This shim is kept to avoid breaking any user code that may have
    imported helpers directly.
"""
from fastapi_flare.storage import FlareStorageProtocol, make_storage  # noqa: F401
