"""Tests for the MCP_FROZEN_ALIASES pilot guard (Fase 0.D).

During the Transgenia pilot (Fase 0-1), client instances
(natasacv/iho/debarca/debarca-staging) are frozen so only
transgenia.odoo.com is reachable. This test covers the pure helpers
`_frozen_aliases()` and `_alias_is_frozen()`.

The ASGI-level 403 response is covered by manual E2E verification
(see audit/2026-07-08/mcp-odoo-optimizacion/congelamiento_instancias.md).

Run from odoo-rpc-mcp/:
    pytest tests/test_frozen_aliases.py -v
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_TMP_ROOT = Path("/tmp/mcp-test-frozen")
_TMP_ROOT.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("MCP_STORAGE_DIR", str(_TMP_ROOT))
os.environ.setdefault("MCP_SSL_CERTS_DIR", str(_TMP_ROOT / "ssl_certs"))

import server  # noqa: E402


def test_frozen_aliases_unset(monkeypatch):
    monkeypatch.delenv("MCP_FROZEN_ALIASES", raising=False)
    assert server._frozen_aliases() == set()


def test_frozen_aliases_empty_string(monkeypatch):
    monkeypatch.setenv("MCP_FROZEN_ALIASES", "")
    assert server._frozen_aliases() == set()


def test_frozen_aliases_whitespace_only(monkeypatch):
    monkeypatch.setenv("MCP_FROZEN_ALIASES", "   ,  , ")
    assert server._frozen_aliases() == set()


def test_frozen_aliases_single(monkeypatch):
    monkeypatch.setenv("MCP_FROZEN_ALIASES", "natasacv")
    assert server._frozen_aliases() == {"natasacv"}


def test_frozen_aliases_multiple_with_spaces(monkeypatch):
    monkeypatch.setenv(
        "MCP_FROZEN_ALIASES",
        "natasacv, iho ,debarca,debarca-staging",
    )
    assert server._frozen_aliases() == {
        "natasacv", "iho", "debarca", "debarca-staging",
    }


def test_alias_is_frozen_matches(monkeypatch):
    monkeypatch.setenv("MCP_FROZEN_ALIASES", "natasacv,iho,debarca")
    assert server._alias_is_frozen("natasacv") is True
    assert server._alias_is_frozen("iho") is True
    assert server._alias_is_frozen("debarca") is True


def test_alias_is_frozen_pilot_survives(monkeypatch):
    """The pilot alias (transgenia_v19) must NEVER be blocked."""
    monkeypatch.setenv("MCP_FROZEN_ALIASES", "natasacv,iho,debarca,debarca-staging")
    assert server._alias_is_frozen("transgenia_v19") is False


def test_alias_is_frozen_empty_alias(monkeypatch):
    """Empty alias never counts as frozen (avoids false-positive rejects)."""
    monkeypatch.setenv("MCP_FROZEN_ALIASES", "natasacv")
    assert server._alias_is_frozen("") is False


def test_alias_is_frozen_no_env(monkeypatch):
    """With env unset, nothing is frozen (safe default)."""
    monkeypatch.delenv("MCP_FROZEN_ALIASES", raising=False)
    assert server._alias_is_frozen("natasacv") is False
    assert server._alias_is_frozen("transgenia_v19") is False


def test_alias_is_frozen_case_sensitive(monkeypatch):
    """Aliases are case-sensitive (matches Odoo alias convention)."""
    monkeypatch.setenv("MCP_FROZEN_ALIASES", "natasacv")
    assert server._alias_is_frozen("NatasaCV") is False
    assert server._alias_is_frozen("natasacv") is True
