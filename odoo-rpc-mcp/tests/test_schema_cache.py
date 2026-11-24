"""Tests for the in-proc schema cache (Fase 0.A).

Covers:
  * hit / miss counting via fetcher call count
  * key includes attributes tuple (order-insensitive)
  * per-alias invalidation
  * global invalidation
  * disabled mode (passthrough) always calls fetcher
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_TMP_ROOT = Path("/tmp/mcp-test-schema-cache")
_TMP_ROOT.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("MCP_STORAGE_DIR", str(_TMP_ROOT))
os.environ.setdefault("MCP_SSL_CERTS_DIR", str(_TMP_ROOT / "ssl_certs"))

import schema_cache  # noqa: E402


@pytest.fixture(autouse=True)
def _clear_cache_between_tests():
    schema_cache.invalidate()
    yield
    schema_cache.invalidate()


def _counting_fetcher(payload):
    calls = {"n": 0}

    def _f():
        calls["n"] += 1
        return payload
    return _f, calls


def test_first_call_is_miss_second_is_hit():
    if not schema_cache.ENABLED:
        pytest.skip("cachetools missing → passthrough mode")
    fetcher, calls = _counting_fetcher({"name": {"string": "Name"}})
    r1 = schema_cache.get_fields("transgenia_v19", "res.partner", ["string"], fetcher)
    r2 = schema_cache.get_fields("transgenia_v19", "res.partner", ["string"], fetcher)
    assert r1 == r2 == {"name": {"string": "Name"}}
    assert calls["n"] == 1, "second call should have hit the cache"


def test_attributes_order_insensitive():
    if not schema_cache.ENABLED:
        pytest.skip("cachetools missing")
    fetcher, calls = _counting_fetcher({"x": 1})
    schema_cache.get_fields("a", "m", ["a", "b"], fetcher)
    schema_cache.get_fields("a", "m", ["b", "a"], fetcher)
    assert calls["n"] == 1, "attributes tuple must be normalized"


@pytest.mark.parametrize("first,second", [("es_MX", "de_DE"), ("de_DE", "es_MX")])
def test_fields_get_tool_caches_labels_per_language(monkeypatch, first, second):
    import server

    # Two cold connections to the same database share an alias, model and attributes.
    monkeypatch.setattr(schema_cache, "ENABLED", True)
    monkeypatch.setenv("ODOO_RPC_DEFAULT_LANG", "user")
    monkeypatch.setattr(server, "_mgr", lambda: None)
    labels = {"es_MX": "Nombre", "de_DE": "Bezeichnung"}
    user_languages = {1: "es_MX", 2: "de_DE"}
    fetched = []

    def execute_kw(db, uid, token, model, method, args, kwargs):
        if method == "context_get":
            return {"lang": user_languages[uid]}
        assert method == "fields_get"
        lang = kwargs["context"]["lang"]
        fetched.append(lang)
        return {"name": {"string": labels[lang]}}

    proxy = Mock()
    proxy.authenticate.side_effect = lambda db, login, token, ctx: int(login)
    proxy.execute_kw.side_effect = execute_kw
    monkeypatch.setattr(server.xmlrpc.client, "ServerProxy", lambda *a, **kw: proxy)
    conns = {
        lang: server.OdooConnection("default", "http://odoo.invalid", "test", str(uid))
        for uid, lang in user_languages.items()
    }
    for lang in (first, second, first, second):
        monkeypatch.setattr(server, "_conn", lambda args, conn=conns[lang]: conn)
        result = server._execute_tool("odoo_fields_get", {
            "model": "demo.item", "attributes": ["string"],
        })
        assert result["name"]["string"] == labels[lang]
    assert fetched == [first, second]
    assert schema_cache.invalidate("default") == 2


def test_different_alias_is_separate_key():
    if not schema_cache.ENABLED:
        pytest.skip("cachetools missing")
    fetcher, calls = _counting_fetcher({"y": 2})
    schema_cache.get_fields("transgenia_v19", "res.partner", None, fetcher)
    schema_cache.get_fields("natasacv", "res.partner", None, fetcher)
    assert calls["n"] == 2, "same model on different alias must miss"


def test_per_alias_invalidate():
    if not schema_cache.ENABLED:
        pytest.skip("cachetools missing")
    fetcher, calls = _counting_fetcher({"z": 3})
    schema_cache.get_fields("transgenia_v19", "res.partner", None, fetcher)
    schema_cache.get_fields("natasacv", "res.partner", None, fetcher)
    n = schema_cache.invalidate("transgenia_v19")
    assert n == 1
    # transgenia entry gone, natasacv still there
    schema_cache.get_fields("transgenia_v19", "res.partner", None, fetcher)
    schema_cache.get_fields("natasacv", "res.partner", None, fetcher)
    assert calls["n"] == 3


def test_global_invalidate():
    if not schema_cache.ENABLED:
        pytest.skip("cachetools missing")
    fetcher, calls = _counting_fetcher({"z": 3})
    schema_cache.get_fields("a", "res.partner", None, fetcher)
    schema_cache.get_fields("b", "res.partner", None, fetcher)
    n = schema_cache.invalidate()
    assert n >= 2
    schema_cache.get_fields("a", "res.partner", None, fetcher)
    assert calls["n"] == 3


def test_name_cache_independent_of_fields_cache():
    if not schema_cache.ENABLED:
        pytest.skip("cachetools missing")
    f_fields, cf = _counting_fetcher([{"id": 1, "name": "Foo"}])
    f_name, cn = _counting_fetcher([[1, "Foo"]])
    schema_cache.get_fields("a", "res.partner", None, f_fields)
    schema_cache.get_name("a", "res.partner", (1,), f_name)
    schema_cache.get_fields("a", "res.partner", None, f_fields)
    schema_cache.get_name("a", "res.partner", (1,), f_name)
    assert cf["n"] == 1
    assert cn["n"] == 1


def test_stats_shape():
    stats = schema_cache.stats()
    assert set(stats.keys()) >= {
        "enabled", "cachetools_available",
        "ttl_seconds", "max_entries",
        "fields_entries", "name_entries",
    }
    assert isinstance(stats["ttl_seconds"], int)


def test_disabled_passes_through(monkeypatch):
    """Force ENABLED=False and verify every call hits the fetcher."""
    monkeypatch.setattr(schema_cache, "ENABLED", False)
    fetcher, calls = _counting_fetcher({"w": 4})
    schema_cache.get_fields("a", "m", None, fetcher)
    schema_cache.get_fields("a", "m", None, fetcher)
    assert calls["n"] == 2, "disabled cache must always call fetcher"
