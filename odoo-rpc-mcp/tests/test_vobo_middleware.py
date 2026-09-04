"""Tests for vobo_middleware.py (Fase 0.E — INACTIVE-by-default scaffold).

Fase 0 covers the CODE only; enforcement is off. Tests verify:
  * canonical 18-tool set
  * preview mode always allows
  * enforce mode requires token OR ejercicio
  * canonical prompt format (BRAIN.md contract)
  * action inference per tool family

Run from odoo-rpc-mcp/:
    pytest tests/test_vobo_middleware.py -v
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_TMP_ROOT = Path("/tmp/mcp-test-vobo")
_TMP_ROOT.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("MCP_STORAGE_DIR", str(_TMP_ROOT))
os.environ.setdefault("MCP_SSL_CERTS_DIR", str(_TMP_ROOT / "ssl_certs"))

import vobo_middleware as vobo  # noqa: E402


CANONICAL_18 = {
    "odoo_create", "odoo_write", "odoo_unlink", "odoo_execute",
    "odoo_message_post", "odoo_attachment_upload",
    "odoo_translate_field", "odoo_translate_html", "odoo_translate_context_aware",
    "odoo_fp_configure", "odoo_fp_remove_action",
    "odoo_stock_product_flip_to_storable", "odoo_stock_mo_delete_draft",
    "odoo_stock_close_unaccounted_value",
    "odoo_stock_initial_import", "odoo_stock_initial_delete",
    "odoo_stock_initial_opening_journal",
    "odoo_record_backup",
}


def test_write_tools_set_is_the_canonical_18():
    assert vobo.WRITE_TOOLS == frozenset(CANONICAL_18)
    assert len(vobo.WRITE_TOOLS) == 18


def test_is_write_tool_positive():
    assert vobo.is_write_tool("odoo_create") is True
    assert vobo.is_write_tool("odoo_stock_initial_import") is True


def test_is_write_tool_negative():
    for name in [
        "odoo_read", "odoo_search_read", "odoo_search_count",
        "odoo_fields_get", "odoo_export_records_json",
        "odoo_export_records_csv", "odoo_refresh",
        "user_connection_add",  # config-mgmt, NOT Odoo data write
    ]:
        assert vobo.is_write_tool(name) is False, name


def test_enforce_disabled_by_default(monkeypatch):
    monkeypatch.delenv("MCP_ENFORCE_VOBO", raising=False)
    assert vobo.enforce_enabled() is False


def test_enforce_reads_env_each_call(monkeypatch):
    monkeypatch.setenv("MCP_ENFORCE_VOBO", "1")
    assert vobo.enforce_enabled() is True
    monkeypatch.setenv("MCP_ENFORCE_VOBO", "false")
    assert vobo.enforce_enabled() is False
    monkeypatch.setenv("MCP_ENFORCE_VOBO", "YES")
    assert vobo.enforce_enabled() is True


def test_check_non_write_always_ok():
    r = vobo.check_vobo("odoo_read", {"model": "res.partner"})
    assert r.ok is True
    assert r.reason == "not-a-write-tool"


def test_check_write_preview_mode_allows(monkeypatch):
    monkeypatch.setenv("MCP_ENFORCE_VOBO", "0")
    r = vobo.check_vobo("odoo_write", {"model": "res.partner", "ids": [1, 2]})
    assert r.ok is True
    assert r.reason == "preview-only"
    assert r.enforcement is False
    assert "APPROVE ODOO WRITE" in (r.prompt or "")


def test_check_write_enforce_missing_token_denies(monkeypatch):
    monkeypatch.setenv("MCP_ENFORCE_VOBO", "1")
    r = vobo.check_vobo("odoo_write", {"model": "res.partner", "ids": [1]})
    assert r.ok is False
    assert r.reason == "missing-token-and-ejercicio"
    assert r.enforcement is True
    err = r.as_error()
    assert "vobo_prompt" in err
    assert "X-Odoo-VoBo" in err["hint"]


def test_check_write_enforce_with_header_token(monkeypatch):
    monkeypatch.setenv("MCP_ENFORCE_VOBO", "1")
    r = vobo.check_vobo(
        "odoo_unlink",
        {"model": "res.partner", "ids": [7]},
        headers={"X-Odoo-VoBo": "sig-abc123"},
    )
    assert r.ok is True
    assert r.reason == "token-present-scaffold"


def test_check_write_enforce_with_ejercicio_arg(monkeypatch):
    monkeypatch.setenv("MCP_ENFORCE_VOBO", "1")
    r = vobo.check_vobo(
        "odoo_create",
        {"model": "res.partner", "vobo_ejercicio_id": "2026-07-08-partner-merge"},
    )
    assert r.ok is True


def test_format_vobo_prompt_shape():
    p = vobo.format_vobo_prompt(
        "2026-07-08-merge", "res.partner", [606, 607], "merge",
    )
    assert p == "APPROVE ODOO WRITE: 2026-07-08-merge | res.partner | 606,607 | merge"


def test_format_vobo_prompt_missing_ids_becomes_new():
    p = vobo.format_vobo_prompt("ej", "sale.order", None, "create")
    assert "| (new) |" in p
    p2 = vobo.format_vobo_prompt("ej", "sale.order", [], "create")
    assert "| (new) |" in p2


def test_format_vobo_prompt_single_int_id():
    p = vobo.format_vobo_prompt("ej", "res.partner", 42, "update")
    assert "| 42 |" in p


def test_action_inference():
    assert vobo._infer_action("odoo_create") == "create"
    assert vobo._infer_action("odoo_write") == "update"
    assert vobo._infer_action("odoo_unlink") == "delete"
    assert vobo._infer_action("odoo_message_post") == "post-chatter"
    assert vobo._infer_action("odoo_attachment_upload") == "upload-attachment"
    assert vobo._infer_action("odoo_translate_html") == "translate"
    assert vobo._infer_action("odoo_fp_configure") == "configure-fiscal-position"
    assert vobo._infer_action("odoo_stock_initial_import") == "stock-adjust"
    assert vobo._infer_action("odoo_record_backup") == "backup"


def test_stats_shape():
    st = vobo.stats()
    assert set(st.keys()) >= {
        "enforce_enabled", "write_tools_count",
        "token_header", "ejercicio_arg_key",
    }
    assert st["write_tools_count"] == 18


def test_custom_header_env(monkeypatch):
    monkeypatch.setenv("MCP_VOBO_TOKEN_HEADER", "X-Custom-VoBo")
    monkeypatch.setenv("MCP_ENFORCE_VOBO", "1")
    r = vobo.check_vobo(
        "odoo_write",
        {"model": "res.partner", "ids": [1]},
        headers={"X-Custom-VoBo": "sig"},
    )
    assert r.ok is True


def test_metrics_emitter_survives_missing_prometheus(monkeypatch):
    """If metrics module or prometheus_client is missing, emitters must
    not raise (best-effort behaviour)."""
    # Simulate a missing metrics import by breaking sys.modules briefly.
    import sys as _sys
    saved = _sys.modules.pop("metrics", None)
    try:
        _sys.modules["metrics"] = None  # ImportError on `import metrics`
        vobo._emit_preview_metric("odoo_write", would_block=True)
        vobo._emit_denial_metric("odoo_write")
    finally:
        if saved is not None:
            _sys.modules["metrics"] = saved
        else:
            _sys.modules.pop("metrics", None)
