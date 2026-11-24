"""Default read language of OdooConnection (Transgenia, 2026-11-23).

Odoo returns translatable fields in en_US when the RPC context carries no
`lang`, whatever the API user's language is. OdooConnection resolves the
API user's language once per connection (res.users.context_get right after
authenticate) and adds it to the context of read methods only.

Run from odoo-rpc-mcp/:
    pytest tests/test_default_lang.py -v
"""
from __future__ import annotations

import logging
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_TMP_ROOT = Path("/tmp/mcp-test-default-lang")
_TMP_ROOT.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("MCP_STORAGE_DIR", str(_TMP_ROOT))
os.environ.setdefault("MCP_SSL_CERTS_DIR", str(_TMP_ROOT / "ssl_certs"))

import server  # noqa: E402


class _FakeOdoo:
    """Stands in for /xmlrpc/2/common and /xmlrpc/2/object; records calls."""

    def __init__(self, user_lang="es_MX", context_get_error=None):
        self.user_lang = user_lang
        self.context_get_error = context_get_error
        self.calls: list[tuple] = []  # (model, method, args, kwargs)

    def server_proxy(self, url, *a, **kw):
        fake = self

        class _Proxy:
            def version(self):
                return {"server_version": "19.0"}

            def authenticate(self, db, login, token, ctx):
                return 7

            def execute_kw(self, db, uid, token, model, method, args, kwargs):
                fake.calls.append((model, method, args, kwargs))
                if method == "context_get":
                    if fake.context_get_error:
                        raise fake.context_get_error
                    return {"lang": fake.user_lang, "tz": "America/Mexico_City", "uid": 7}
                if method == "fields_get":
                    return {"name": {"type": "char", "translate": True, "string": "Name"}}
                if method == "search_read" and model == "res.lang":
                    return [{"code": "es_MX", "name": "Spanish (MX)"},
                            {"code": "de_DE", "name": "German"}]
                if method == "read":
                    return [{"id": i, "name": "Consultoría Odoo"} for i in args[0]]
                if method == "get_field_translations":
                    return [[{"lang": "en_US", "source": "x", "value": "x"}], {}]
                if method in ("search", "search_read"):
                    return []
                return True

        return _Proxy()

    def data_calls(self):
        return [c for c in self.calls if c[1] != "context_get"]

    def context_get_count(self):
        return sum(1 for c in self.calls if c[1] == "context_get")


@pytest.fixture
def fake(monkeypatch):
    f = _FakeOdoo()
    monkeypatch.setattr(server.xmlrpc.client, "ServerProxy", f.server_proxy)
    monkeypatch.delenv("ODOO_RPC_DEFAULT_LANG", raising=False)
    return f


def _conn():
    return server.OdooConnection(
        alias="t", url="https://odoo.example", db="test",
        username="api@example.com", api_key="K", protocol="xmlrpc",
        verify_ssl=True,
    )


# ── Reads without lang get the API user's language ─────────────

def test_read_without_lang_uses_user_lang(fake):
    conn = _conn()
    conn.execute_kw("product.template", "search_read", [[]], {"fields": ["name"]})
    assert fake.data_calls() == [
        ("product.template", "search_read", [[]],
         {"fields": ["name"], "context": {"lang": "es_MX"}}),
    ]


def test_context_get_called_once_per_connection(fake):
    conn = _conn()
    for _ in range(3):
        conn.execute_kw("product.template", "read", [[1]], {"fields": ["name"]})
    assert fake.context_get_count() == 1


def test_concurrent_first_reads_wait_for_language(fake, monkeypatch):
    conn = _conn()
    resolving = Event()
    second_reader_reached = Event()
    dispatch = conn._dispatch_kw

    def observe_dispatch(model, method, args, kwargs):
        if method == "context_get":
            resolving.set()
            assert second_reader_reached.wait(5)
        else:
            # A premature read has already chosen its outgoing context.
            second_reader_reached.set()
        return dispatch(model, method, args, kwargs)

    monkeypatch.setattr(conn, "_dispatch_kw", observe_dispatch)
    if hasattr(conn, "_auth_lock"):
        lock = conn._auth_lock

        class ObservedLock:
            def __enter__(self):
                if not lock.acquire(blocking=False):
                    # The second reader reached initialization and must wait.
                    second_reader_reached.set()
                    assert lock.acquire(timeout=5)

            def __exit__(self, *exc):
                lock.release()

        monkeypatch.setattr(conn, "_auth_lock", ObservedLock())

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(conn.execute_kw, "product.template", "read", [[1]])
        assert resolving.wait(5)
        second = pool.submit(conn.execute_kw, "product.template", "read", [[2]])
        first.result(timeout=10)
        second.result(timeout=10)

    assert fake.context_get_count() == 1
    assert [call[3] for call in fake.data_calls()] == [{"context": {"lang": "es_MX"}}] * 2


@pytest.mark.parametrize("method", sorted(server._READ_METHODS_DEFAULT_LANG))
def test_every_read_method_gets_lang(fake, method):
    conn = _conn()
    conn.execute_kw("product.template", method, [[]], {})
    assert fake.data_calls()[-1][3] == {"context": {"lang": "es_MX"}}


def test_other_context_keys_are_kept_and_caller_dict_not_mutated(fake):
    conn = _conn()
    kwargs = {"fields": ["name"], "context": {"active_test": False}}
    conn.execute_kw("product.template", "search_read", [[]], kwargs)
    sent = fake.data_calls()[-1][3]
    assert sent["context"] == {"active_test": False, "lang": "es_MX"}
    assert kwargs == {"fields": ["name"], "context": {"active_test": False}}


# ── An explicit lang always wins ───────────────────────────────

@pytest.mark.parametrize("explicit", ["en_US", "de_DE", False, None])
def test_explicit_lang_is_respected(fake, explicit):
    conn = _conn()
    conn.execute_kw("product.template", "read", [[1]],
                    {"fields": ["name"], "context": {"lang": explicit}})
    assert fake.data_calls()[-1][3] == {"fields": ["name"], "context": {"lang": explicit}}


# ── Writes and other methods are never touched ─────────────────

@pytest.mark.parametrize("method,args,kwargs", [
    ("write", [[1], {"name": "Nuevo"}], {}),
    ("create", [{"name": "Nuevo"}], {}),
    ("unlink", [[1]], {}),
    ("action_confirm", [[1]], {}),
    ("update_field_translations", [[1], "name", {"en_US": "New"}], {}),
    ("get_field_translations", [[1], "name"], {}),
    ("write", [[1], {"name": "x"}], {"context": {"tracking_disable": True}}),
])
def test_write_and_other_methods_get_no_new_context(fake, method, args, kwargs):
    conn = _conn()
    conn.execute_kw("product.template", method, args, dict(kwargs))
    assert fake.data_calls()[-1] == ("product.template", method, args, kwargs)


# ── Switches ───────────────────────────────────────────────────

def test_off_restores_previous_behaviour(fake, monkeypatch):
    monkeypatch.setenv("ODOO_RPC_DEFAULT_LANG", "off")
    conn = _conn()
    conn.execute_kw("product.template", "search_read", [[]], {"fields": ["name"]})
    assert fake.context_get_count() == 0
    assert fake.data_calls()[-1][3] == {"fields": ["name"]}


def test_fixed_language_skips_context_get(fake, monkeypatch):
    monkeypatch.setenv("ODOO_RPC_DEFAULT_LANG", "de_DE")
    conn = _conn()
    conn.execute_kw("product.template", "read", [[1]], {})
    assert fake.context_get_count() == 0
    assert fake.data_calls()[-1][3] == {"context": {"lang": "de_DE"}}


def test_context_get_failure_keeps_previous_behaviour(monkeypatch, caplog):
    f = _FakeOdoo(context_get_error=Exception("Access denied"))
    monkeypatch.setattr(server.xmlrpc.client, "ServerProxy", f.server_proxy)
    monkeypatch.delenv("ODOO_RPC_DEFAULT_LANG", raising=False)
    conn = _conn()
    with caplog.at_level(logging.WARNING, logger=server.logger.name):
        assert conn.authenticate() == 7
        conn.execute_kw("product.template", "read", [[1]], {})
    assert f.data_calls()[-1][3] == {}
    assert any("context_get failed" in r.getMessage() for r in caplog.records)


def test_preset_uid_without_authenticate_adds_nothing(fake):
    """Connections whose uid was set without authenticate() (as the routing
    tests do) keep the previous behaviour."""
    conn = _conn()
    conn._uid = 7
    conn.execute_kw("product.template", "read", [[1]], {})
    assert fake.context_get_count() == 0
    assert fake.data_calls()[-1][3] == {}


# ── Through the MCP tools ──────────────────────────────────────

@pytest.fixture
def tool_conn(fake, monkeypatch):
    conn = _conn()
    monkeypatch.setattr(server, "_conn", lambda args: conn)
    monkeypatch.setattr(server, "_mgr", lambda: SimpleNamespace(get=lambda alias: None))
    return conn


def test_odoo_read_tool_reads_in_user_lang(fake, tool_conn):
    server._execute_tool("odoo_read", {"model": "product.template", "ids": [178],
                                       "fields": ["name"]})
    assert fake.data_calls()[-1] == (
        "product.template", "read", [[178]],
        {"fields": ["name"], "context": {"lang": "es_MX"}},
    )


def test_odoo_execute_tool_respects_caller_lang(fake, tool_conn):
    server._execute_tool("odoo_execute", {
        "model": "product.template", "method": "search_read",
        "args": [[["id", "=", 178]]],
        "kwargs": {"fields": ["name"], "context": {"lang": "en_US"}},
    })
    assert fake.data_calls()[-1][3] == {"fields": ["name"], "context": {"lang": "en_US"}}


def _translate_field_calls(monkeypatch, mode):
    f = _FakeOdoo()
    monkeypatch.setattr(server.xmlrpc.client, "ServerProxy", f.server_proxy)
    if mode is None:
        monkeypatch.delenv("ODOO_RPC_DEFAULT_LANG", raising=False)
    else:
        monkeypatch.setenv("ODOO_RPC_DEFAULT_LANG", mode)
    conn = _conn()
    monkeypatch.setattr(server, "_conn", lambda args: conn)
    monkeypatch.setattr(server, "_mgr", lambda: SimpleNamespace(get=lambda alias: None))
    result = server._execute_tool("odoo_translate_field", {
        "model": "product.template", "res_id": 178, "field_name": "name",
        "translations": {"es_MX": "Viático Prospección", "de_DE": "Reisekosten"},
        "source_lang": "en_US", "mark_identical_as_translated": False,
    })
    assert result.get("success") is True, result
    return f.data_calls()


def test_odoo_translate_field_calls_are_unchanged(monkeypatch):
    """The translation tool sends exactly the same RPCs with the default
    language on (user's es_MX) as with it off (behaviour before)."""
    before = _translate_field_calls(monkeypatch, "off")
    after = _translate_field_calls(monkeypatch, None)
    assert after == before
    # and the explicit source-language read is still there, untouched
    assert ("product.template", "read", [[178], ["name"]],
            {"context": {"lang": "en_US"}}) in after


def test_exemption_does_not_leak_to_the_next_tool(fake, tool_conn):
    server._execute_tool("odoo_get_field_translations", {
        "model": "product.template", "res_id": 178, "field_name": "name",
    })
    assert server._default_lang_disabled.get() is False
    server._execute_tool("odoo_read", {"model": "product.template", "ids": [178],
                                       "fields": ["name"]})
    assert fake.data_calls()[-1][3]["context"] == {"lang": "es_MX"}
