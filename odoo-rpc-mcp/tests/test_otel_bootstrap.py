"""Tests for otel_bootstrap.py (Fase 0.C).

Exercises the graceful-degrade paths (env off, endpoint empty, otel
absent — the latter is simulated by patching the import helper) and
the live path with an InMemorySpanExporter.

Run from odoo-rpc-mcp/:
    pytest tests/test_otel_bootstrap.py -v
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_TMP_ROOT = Path("/tmp/mcp-test-otel")
_TMP_ROOT.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("MCP_STORAGE_DIR", str(_TMP_ROOT))
os.environ.setdefault("MCP_SSL_CERTS_DIR", str(_TMP_ROOT / "ssl_certs"))

import otel_bootstrap  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_state():
    otel_bootstrap._reset_for_tests()
    yield
    otel_bootstrap._reset_for_tests()


def test_disabled_when_env_off(monkeypatch):
    monkeypatch.setenv("OTEL_ENABLED", "0")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://x/y")
    assert otel_bootstrap.init(force=True) is False
    assert otel_bootstrap.is_enabled() is False
    assert otel_bootstrap.stats()["reason_disabled"] == "OTEL_ENABLED=0"


def test_disabled_when_endpoint_empty(monkeypatch):
    monkeypatch.setenv("OTEL_ENABLED", "1")
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    assert otel_bootstrap.init(force=True) is False
    assert "endpoint-empty" in otel_bootstrap.stats()["reason_disabled"].lower()


def test_disabled_when_opentelemetry_missing(monkeypatch):
    monkeypatch.setenv("OTEL_ENABLED", "1")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://x/y")
    monkeypatch.setattr(otel_bootstrap, "_try_import_otel", lambda: None)
    assert otel_bootstrap.init(force=True) is False
    assert otel_bootstrap.stats()["reason_disabled"] == "opentelemetry-not-installed"


def test_start_span_noop_when_disabled(monkeypatch):
    monkeypatch.setenv("OTEL_ENABLED", "0")
    otel_bootstrap.init(force=True)
    # start_span must be safe to call even without a tracer
    with otel_bootstrap.start_span("whatever", **{"foo": "bar"}) as span:
        assert span is None


def test_parse_headers():
    parsed = otel_bootstrap._parse_headers(
        "Authorization=Bearer abc, X-Env=prod,   ,Bad="
    )
    assert parsed["Authorization"] == "Bearer abc"
    assert parsed["X-Env"] == "prod"
    assert parsed["Bad"] == ""


def test_parse_headers_empty():
    assert otel_bootstrap._parse_headers("") == {}
    assert otel_bootstrap._parse_headers("   ") == {}


def _make_inmem_tracer():
    """Force otel_bootstrap into 'live' mode using an in-memory exporter."""
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )
    from opentelemetry import trace

    exporter = InMemorySpanExporter()
    resource = Resource.create({"service.name": "mcp-odoo-rpc-test"})
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test")
    otel_bootstrap._STATE["initialized"] = True
    otel_bootstrap._STATE["enabled"] = True
    otel_bootstrap._STATE["endpoint"] = "in-mem"
    otel_bootstrap._STATE["service_name"] = "mcp-odoo-rpc-test"
    otel_bootstrap._STATE["provider"] = provider
    otel_bootstrap._STATE["tracer"] = tracer
    otel_bootstrap._STATE["reason_disabled"] = ""
    return exporter


def test_start_span_emits_when_enabled():
    exporter = _make_inmem_tracer()
    with otel_bootstrap.start_span(
        "odoo.execute_kw",
        **{"odoo.model": "res.partner", "odoo.method": "search",
           "odoo.alias": "transgenia_v19"},
    ) as span:
        assert span is not None
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    s = spans[0]
    assert s.name == "odoo.execute_kw"
    assert s.attributes["odoo.model"] == "res.partner"
    assert s.attributes["odoo.method"] == "search"
    assert s.attributes["odoo.alias"] == "transgenia_v19"


def test_start_span_records_exception():
    exporter = _make_inmem_tracer()
    with pytest.raises(RuntimeError):
        with otel_bootstrap.start_span("boom"):
            raise RuntimeError("kaboom")
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    s = spans[0]
    assert s.attributes.get("error") is True
    assert s.attributes.get("error.type") == "RuntimeError"


def test_start_span_drops_none_attributes():
    exporter = _make_inmem_tracer()
    with otel_bootstrap.start_span("t", **{"real": "yes", "missing": None}):
        pass
    s = exporter.get_finished_spans()[0]
    assert s.attributes["real"] == "yes"
    assert "missing" not in s.attributes


def test_stats_shape():
    st = otel_bootstrap.stats()
    assert set(st.keys()) >= {
        "initialized", "enabled", "service_name",
        "endpoint", "reason_disabled",
    }


def test_init_is_idempotent(monkeypatch):
    monkeypatch.setenv("OTEL_ENABLED", "0")
    assert otel_bootstrap.init() is False
    # second call without force must NOT flip state
    monkeypatch.setenv("OTEL_ENABLED", "1")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://x")
    assert otel_bootstrap.init() is False  # cached "disabled" result
    assert otel_bootstrap.stats()["reason_disabled"] == "OTEL_ENABLED=0"
