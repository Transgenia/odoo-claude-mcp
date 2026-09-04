"""Tests for odoo_export_records_json / odoo_export_records_csv (Fase 0.B).

Declarative read-only export wrappers around ORM `export_data`. Handler is
downstream of `_conn(args)`, so tests inject a fake connection whose
`execute_kw` returns canned rows.

Run from odoo-rpc-mcp/:
    pytest tests/test_export_records.py -v
"""
from __future__ import annotations

import base64
import csv
import io
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_TMP_ROOT = Path("/tmp/mcp-test-export")
_TMP_ROOT.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("MCP_STORAGE_DIR", str(_TMP_ROOT))
os.environ.setdefault("MCP_SSL_CERTS_DIR", str(_TMP_ROOT / "ssl_certs"))

import server  # noqa: E402


class _FakeConn:
    """Minimal stand-in for OdooConnection. Records calls and returns canned data."""

    def __init__(self, ids=None, rows=None):
        self.alias = "transgenia_v19"
        self.url = "https://example.invalid"
        self._ids = ids if ids is not None else [10, 20, 30]
        self._rows = rows if rows is not None else [
            ["Alice", "alice@example.com"],
            ["Bob", "bob@example.com"],
            ["Chi", "chi@example.com"],
        ]
        self.calls = []

    def execute_kw(self, model, method, args, kwargs=None):
        self.calls.append((model, method, args, kwargs or {}))
        if method == "search":
            return list(self._ids)
        if method == "export_data":
            return {"datas": list(self._rows)}
        raise AssertionError(f"Unexpected method {method}")


@pytest.fixture(autouse=True)
def _stub_mgr(monkeypatch):
    """`_execute_tool` calls `_mgr()` before dispatching; stub it so the
    test doesn't need a real ConnectionManager on disk."""
    monkeypatch.setattr(server, "_mgr", lambda: SimpleNamespace(get=lambda alias: None))


@pytest.fixture
def fake_conn(monkeypatch):
    conn = _FakeConn()
    monkeypatch.setattr(server, "_conn", lambda args: conn)
    return conn


def test_json_export_returns_dicts(fake_conn):
    result = server._execute_tool("odoo_export_records_json", {
        "connection": "transgenia_v19",
        "model": "res.partner",
        "fields": ["name", "email"],
        "domain": [["is_company", "=", True]],
    })
    assert result["model"] == "res.partner"
    assert result["fields"] == ["name", "email"]
    assert result["count"] == 3
    assert result["records"][0] == {"name": "Alice", "email": "alice@example.com"}
    assert result["records"][2] == {"name": "Chi", "email": "chi@example.com"}


def test_json_export_empty_returns_zero(monkeypatch):
    conn = _FakeConn(ids=[], rows=[])
    monkeypatch.setattr(server, "_conn", lambda args: conn)
    result = server._execute_tool("odoo_export_records_json", {
        "model": "res.partner",
        "fields": ["name"],
    })
    assert result == {
        "model": "res.partner", "fields": ["name"],
        "count": 0, "records": [],
    }
    # Only the search should have been made — no export_data on empty ids
    assert [c[1] for c in conn.calls] == ["search"]


def test_json_export_missing_fields_returns_error(fake_conn):
    result = server._execute_tool("odoo_export_records_json", {
        "model": "res.partner", "fields": [],
    })
    assert "error" in result


def test_csv_export_has_header_and_rows(fake_conn):
    result = server._execute_tool("odoo_export_records_csv", {
        "model": "res.partner",
        "fields": ["name", "email"],
    })
    assert result["model"] == "res.partner"
    assert result["count"] == 3
    assert "content_base64" in result
    content = base64.b64decode(result["content_base64"]).decode("utf-8")
    reader = list(csv.reader(io.StringIO(content)))
    assert reader[0] == ["name", "email"]
    assert reader[1] == ["Alice", "alice@example.com"]
    assert len(reader) == 4  # header + 3 rows


def test_csv_export_empty(monkeypatch):
    conn = _FakeConn(ids=[], rows=[])
    monkeypatch.setattr(server, "_conn", lambda args: conn)
    result = server._execute_tool("odoo_export_records_csv", {
        "model": "res.partner", "fields": ["name"],
    })
    assert result["count"] == 0
    assert result["content_base64"] == ""
    assert result["size"] == 0


def test_csv_export_saves_to_path(monkeypatch, tmp_path):
    conn = _FakeConn()
    monkeypatch.setattr(server, "_conn", lambda args: conn)
    out = tmp_path / "export.csv"
    result = server._execute_tool("odoo_export_records_csv", {
        "model": "res.partner",
        "fields": ["name", "email"],
        "save_path": str(out),
    })
    assert result["status"] == "saved"
    assert result["path"] == str(out)
    assert out.exists()
    content = out.read_text(encoding="utf-8")
    assert content.startswith("name,email\n")
    assert "Alice,alice@example.com" in content


def test_csv_normalises_false_and_none(monkeypatch):
    conn = _FakeConn(
        ids=[1, 2],
        rows=[["Alice", False], ["Bob", None]],
    )
    monkeypatch.setattr(server, "_conn", lambda args: conn)
    result = server._execute_tool("odoo_export_records_csv", {
        "model": "res.partner", "fields": ["name", "email"],
    })
    content = base64.b64decode(result["content_base64"]).decode("utf-8")
    rows = list(csv.reader(io.StringIO(content)))
    assert rows[1] == ["Alice", ""]  # False -> empty
    assert rows[2] == ["Bob", ""]    # None -> empty


def test_search_receives_limit_offset_order(fake_conn):
    server._execute_tool("odoo_export_records_json", {
        "model": "res.partner",
        "fields": ["name"],
        "limit": 25, "offset": 5, "order": "name asc",
    })
    model, method, args, kwargs = fake_conn.calls[0]
    assert method == "search"
    assert kwargs == {"limit": 25, "offset": 5, "order": "name asc"}


def test_export_data_no_import_compat_kwarg_json(fake_conn):
    # Odoo 19 CE: export_data() rejects import_compat — handler must NOT pass it.
    server._execute_tool("odoo_export_records_json", {
        "model": "res.partner",
        "fields": ["name"],
        "import_compat": True,  # accepted in schema but must be ignored on v19
    })
    _, method, _, kwargs = fake_conn.calls[1]
    assert method == "export_data"
    assert kwargs in (None, {})


def test_export_data_no_import_compat_kwarg_csv(fake_conn):
    server._execute_tool("odoo_export_records_csv", {
        "model": "res.partner",
        "fields": ["name"],
    })
    _, method, _, kwargs = fake_conn.calls[1]
    assert method == "export_data"
    assert kwargs in (None, {})
