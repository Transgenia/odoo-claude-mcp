"""In-process schema cache (Fase 0.A).

Caches results of expensive ``fields_get`` / ``name_get`` calls keyed
by (alias, model, attributes_tuple). Bounded by TTL and max entries.

Design choices (see plan.md §Fase 0.A):
  * In-proc, no Redis. Adds one dep (cachetools) that is well-scoped.
  * If ``cachetools`` is missing, the module degrades to a passthrough
    (cache disabled) — behaviour unchanged, only slower.
  * Metrics are emitted through ``metrics.py`` if the counters exist.
    The module imports ``metrics`` lazily and swallows AttributeError
    to survive older metric surfaces.

Config (env vars):
  MCP_SCHEMA_CACHE_ENABLED   "1" | "0"   default "1"
  MCP_SCHEMA_CACHE_TTL       seconds     default 900 (15 min)
  MCP_SCHEMA_CACHE_MAX       entries     default 500
"""
from __future__ import annotations

import logging
import os
import threading
from typing import Any, Callable, Iterable, Optional

logger = logging.getLogger("schema_cache")

_TTL = int(os.environ.get("MCP_SCHEMA_CACHE_TTL", "900"))
_MAX = int(os.environ.get("MCP_SCHEMA_CACHE_MAX", "500"))
_ENABLED_ENV = os.environ.get("MCP_SCHEMA_CACHE_ENABLED", "1") == "1"

try:
    from cachetools import TTLCache
    _fields_cache: Any = TTLCache(maxsize=_MAX, ttl=_TTL)
    _name_cache: Any = TTLCache(maxsize=_MAX, ttl=_TTL)
    _AVAILABLE = True
except ImportError:
    _fields_cache = {}
    _name_cache = {}
    _AVAILABLE = False
    logger.warning(
        "cachetools not installed; schema cache disabled (passthrough)."
    )

ENABLED = _ENABLED_ENV and _AVAILABLE

_lock = threading.Lock()


def _emit(cache_name: str, hit: bool) -> None:
    """Best-effort metric emission. Never raises."""
    try:
        import metrics as _m
    except Exception:  # noqa: BLE001
        return
    try:
        if hit:
            _m.observe_schema_cache_hit(cache_name)
        else:
            _m.observe_schema_cache_miss(cache_name)
    except AttributeError:
        pass  # metrics surface older than 2.27
    except Exception as exc:  # noqa: BLE001
        logger.debug("metric emit failed: %s", exc)


def _norm_attrs(attributes: Optional[Iterable[str]]) -> tuple:
    if not attributes:
        return ()
    return tuple(sorted(attributes))


def get_fields(
    alias: str,
    model: str,
    attributes: Optional[Iterable[str]],
    fetcher: Callable[[], Any],
) -> Any:
    """Cache-through wrapper around a ``fields_get`` call.

    ``fetcher`` is called only on cache miss. Its result is stored
    verbatim. On any failure the call falls through to ``fetcher()``.
    """
    if not ENABLED:
        _emit("fields", False)
        return fetcher()
    key = (alias, model, _norm_attrs(attributes))
    with _lock:
        val = _fields_cache.get(key)
    if val is not None:
        _emit("fields", True)
        return val
    result = fetcher()
    with _lock:
        _fields_cache[key] = result
    _emit("fields", False)
    return result


def get_name(
    alias: str,
    model: str,
    ids_tuple: tuple,
    fetcher: Callable[[], Any],
) -> Any:
    """Cache-through wrapper around a ``name_get`` call.

    ``ids_tuple`` MUST be a hashable tuple of ints.
    """
    if not ENABLED:
        _emit("name", False)
        return fetcher()
    key = (alias, model, ids_tuple)
    with _lock:
        val = _name_cache.get(key)
    if val is not None:
        _emit("name", True)
        return val
    result = fetcher()
    with _lock:
        _name_cache[key] = result
    _emit("name", False)
    return result


def invalidate(alias: Optional[str] = None) -> int:
    """Drop cached entries.

    ``alias=None`` clears everything. Returns number of entries removed.
    Wired to the ``odoo_refresh`` tool to keep the cache honest after
    an operator-initiated refresh.
    """
    with _lock:
        if alias is None:
            n = len(_fields_cache) + len(_name_cache)
            _fields_cache.clear()
            _name_cache.clear()
            return n
        keys_f = [k for k in list(_fields_cache) if k[0] == alias]
        keys_n = [k for k in list(_name_cache) if k[0] == alias]
        for k in keys_f:
            _fields_cache.pop(k, None)
        for k in keys_n:
            _name_cache.pop(k, None)
        return len(keys_f) + len(keys_n)


def stats() -> dict:
    """Snapshot for debugging / /health payload."""
    with _lock:
        return {
            "enabled": ENABLED,
            "cachetools_available": _AVAILABLE,
            "ttl_seconds": _TTL,
            "max_entries": _MAX,
            "fields_entries": len(_fields_cache),
            "name_entries": len(_name_cache),
        }
