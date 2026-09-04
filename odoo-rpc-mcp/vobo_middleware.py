"""VoBo middleware scaffold for Odoo WRITE tools (Fase 0.E — INACTIVE).

Fase 0 delivers the CODE only: the middleware is loaded, WRITE tools
are catalogued, preview logs are emitted, but enforcement is OFF by
default (`MCP_ENFORCE_VOBO=false`). Fase 2.A flips the flag after
Saurat's VoBo on the token semantics (see plan §11 D4).

Environment
-----------
    MCP_ENFORCE_VOBO         "0" (default, preview-only) or "1" (Fase 2.A)
    MCP_VOBO_TOKEN_HEADER    header name where callers pass the VoBo token
                             (default: X-Odoo-VoBo)
    MCP_VOBO_EJERCICIO_KEY   arg key holding the ejercicio_id
                             (default: vobo_ejercicio_id)

Public API
----------
    WRITE_TOOLS: frozenset[str]                    # canonical 18-tool set
    is_write_tool(name: str) -> bool
    check_vobo(tool, args, headers=None) -> Result # (ok, reason, prompt|None)
    format_vobo_prompt(...)                        # canonical VoBo prompt
    enforce_enabled() -> bool                      # reads env each call
    stats() -> dict

Brain: fase-0-vobo-middleware — scaffold NOW so Fase 2.A only flips a flag.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Optional

log = logging.getLogger(__name__)

# ── Canonical list of WRITE tools (18) ──────────────────────────────
# These are the Odoo-data-mutating tools. Not included:
#   * odoo_refresh / odoo_cert_refresh — cache/notify, not data writes
#   * user_connection_add/delete/activate — MCP-local config, not Odoo writes
#   * odoo_export_records_* — read-only wrappers around export_data
#
# When adding a new WRITE tool, add it here AND add a test in
# tests/test_vobo_middleware.py::test_write_tools_set_is_the_canonical_18.
WRITE_TOOLS: frozenset[str] = frozenset({
    "odoo_create",
    "odoo_write",
    "odoo_unlink",
    "odoo_execute",
    "odoo_message_post",
    "odoo_attachment_upload",
    "odoo_translate_field",
    "odoo_translate_html",
    "odoo_translate_context_aware",
    "odoo_fp_configure",
    "odoo_fp_remove_action",
    "odoo_stock_product_flip_to_storable",
    "odoo_stock_mo_delete_draft",
    "odoo_stock_close_unaccounted_value",
    "odoo_stock_initial_import",
    "odoo_stock_initial_delete",
    "odoo_stock_initial_opening_journal",
    "odoo_record_backup",
})


@dataclass
class VoBoResult:
    """Outcome of a VoBo check. When enforce is off, ok is always True."""
    ok: bool
    reason: str
    prompt: Optional[str] = None
    enforcement: bool = False   # True when the check was operating in enforce mode

    def as_error(self) -> dict:
        """Serialisable payload for the 403-equivalent JSON response."""
        return {
            "error": "VoBo required for Odoo write",
            "reason": self.reason,
            "vobo_prompt": self.prompt,
            "hint": (
                "Este tool escribe en Odoo. Reenvía la operación incluyendo "
                "el token de VoBo firmado por Saurat en el header "
                f"{token_header_name()} o el arg {ejercicio_arg_key()}."
            ),
        }


def enforce_enabled() -> bool:
    """Read MCP_ENFORCE_VOBO on every call — allows runtime flipping via env."""
    val = (os.environ.get("MCP_ENFORCE_VOBO", "0") or "").strip().lower()
    return val in ("1", "true", "yes", "on")


def token_header_name() -> str:
    return (os.environ.get("MCP_VOBO_TOKEN_HEADER", "") or "X-Odoo-VoBo").strip()


def ejercicio_arg_key() -> str:
    return (os.environ.get("MCP_VOBO_EJERCICIO_KEY", "") or "vobo_ejercicio_id").strip()


def is_write_tool(name: str) -> bool:
    return name in WRITE_TOOLS


def _extract_model(args: dict) -> str:
    return str(args.get("model") or "")


def _extract_ids(args: dict) -> list:
    ids = args.get("ids")
    if isinstance(ids, list):
        return ids
    if isinstance(ids, int):
        return [ids]
    # odoo_create returns ids AFTER the call; here we only see the input vals.
    return []


def format_vobo_prompt(
    ejercicio_id: str, model: str, ids: list | int | None, action: str,
) -> str:
    """Canonical VoBo prompt string per BRAIN.md and plan §6.

    Format:  APPROVE ODOO WRITE: <ejercicio_id> | <model> | <ids> | <action>
    """
    if ids is None:
        ids_str = "(new)"
    elif isinstance(ids, (list, tuple)):
        ids_str = ",".join(str(i) for i in ids) if ids else "(new)"
    else:
        ids_str = str(ids)
    return (
        f"APPROVE ODOO WRITE: {ejercicio_id or '(none)'} | "
        f"{model or '(unknown)'} | {ids_str} | {action}"
    )


def check_vobo(
    tool: str, args: dict, headers: Optional[dict] = None,
) -> VoBoResult:
    """Central gate for a WRITE tool call.

    * enforce OFF (Fase 0-1 default): always returns ok=True with reason
      "preview-only"; caller should log the audit preview and proceed.
    * enforce ON  (Fase 2.A): requires a token in the configured header OR
      the ejercicio arg; missing → ok=False + canonical prompt.
      Token validation itself is Fase 2.A design work (Saurat VoBo D4).
    """
    if not is_write_tool(tool):
        return VoBoResult(ok=True, reason="not-a-write-tool", enforcement=False)

    enforce = enforce_enabled()
    model = _extract_model(args)
    ids = _extract_ids(args)
    action = _infer_action(tool)
    ejercicio_id = str(args.get(ejercicio_arg_key(), ""))
    prompt = format_vobo_prompt(ejercicio_id, model, ids, action)

    if not enforce:
        # Fase 0-1 preview-only: log an audit-trail line so we can size
        # the impact before flipping enforcement.
        log.info(
            "vobo.preview tool=%s model=%s ids=%s ejercicio=%s "
            "(enforcement=off — would %s)",
            tool, model, ids, ejercicio_id or "(none)",
            "REQUIRE VoBo" if not ejercicio_id else "ALLOW (had ejercicio)",
        )
        _emit_preview_metric(tool, would_block=not ejercicio_id)
        return VoBoResult(
            ok=True, reason="preview-only", prompt=prompt, enforcement=False,
        )

    # Enforce mode (Fase 2.A design — Saurat VoBo D4 still pending)
    token = None
    if headers:
        token = headers.get(token_header_name()) or headers.get(
            token_header_name().lower(),
        )
    if not token and not ejercicio_id:
        _emit_denial_metric(tool)
        return VoBoResult(
            ok=False, reason="missing-token-and-ejercicio",
            prompt=prompt, enforcement=True,
        )
    # Token validation lands in Fase 2.A; scaffold accepts presence only.
    return VoBoResult(
        ok=True, reason="token-present-scaffold", prompt=prompt, enforcement=True,
    )


def _infer_action(tool: str) -> str:
    """Human-readable action inferred from the tool name."""
    mapping = {
        "odoo_create": "create",
        "odoo_write": "update",
        "odoo_unlink": "delete",
        "odoo_execute": "execute-arbitrary-method",
        "odoo_message_post": "post-chatter",
        "odoo_attachment_upload": "upload-attachment",
    }
    if tool in mapping:
        return mapping[tool]
    if tool.startswith("odoo_translate_"):
        return "translate"
    if tool.startswith("odoo_fp_"):
        return "configure-fiscal-position"
    if tool.startswith("odoo_stock_"):
        return "stock-adjust"
    if tool == "odoo_record_backup":
        return "backup"
    return "write"


def _emit_preview_metric(tool: str, would_block: bool) -> None:
    try:
        import metrics
        # Only fire if the metric was declared in the current process.
        if hasattr(metrics, "observe_vobo_preview"):
            metrics.observe_vobo_preview(tool, would_block)
    except Exception:
        pass


def _emit_denial_metric(tool: str) -> None:
    try:
        import metrics
        if hasattr(metrics, "observe_vobo_denial"):
            metrics.observe_vobo_denial(tool)
    except Exception:
        pass


def stats() -> dict:
    return {
        "enforce_enabled": enforce_enabled(),
        "write_tools_count": len(WRITE_TOOLS),
        "token_header": token_header_name(),
        "ejercicio_arg_key": ejercicio_arg_key(),
    }
