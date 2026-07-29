"""Tests for `amplifier_module_tool_mcp.sdk_compat`.

Covers the fail-loud `sdk_field` extension (optional-field default) and the
MCP protocol-error surfacing added alongside 2026-07-28 conformance:
`MCP_ERROR_CLASS`, `describe_mcp_error`, and `MCPProtocolError`.
"""

from typing import Any, ClassVar

from mcp.types import ErrorData

from amplifier_module_tool_mcp.sdk_compat import (
    MCP_ERROR_CLASS,
    MCPProtocolError,
    MRTRNotSupportedError,
    describe_mcp_error,
    is_modern_protocol_version,
    is_mrtr_unsupported_error,
    sdk_field,
)

# `MCP_ERROR_CLASS` is typed as `type[Exception]` in sdk_compat.py (so
# `except MCP_ERROR_CLASS as e:` type-checks correctly in client.py). Its
# *constructor*, however, genuinely differs across SDK majors -- that's the
# whole reason this test probes both shapes below -- so it's used through an
# `Any`-typed alias here rather than fighting the (correct) stricter typing
# in production code.
_ErrorClass: Any = MCP_ERROR_CLASS


def _make_mcp_error(code: int, message: str, data: Any = None) -> Exception:
    """Construct an instance of the installed SDK's MCP error class.

    `mcp` 2.x: `MCPError(code, message, data=None)`.
    `mcp` 1.x: `McpError(error: ErrorData)`.
    Both store the result under `.error` (an `ErrorData`), which is what
    `describe_mcp_error` actually reads -- this helper just bridges the
    two constructor shapes so the same tests run on either installed major.
    """
    try:
        return _ErrorClass(code=code, message=message, data=data)  # mcp>=2.0
    except TypeError:
        return _ErrorClass(ErrorData(code=code, message=message, data=data))  # mcp<2.0


# ---------------------------------------------------------------------------
# sdk_field: default= extension
# ---------------------------------------------------------------------------


def test_sdk_field_returns_first_present_name():
    class Obj:
        input_schema: ClassVar[dict] = {"type": "object"}

    assert sdk_field(Obj(), "input_schema", "inputSchema") == {"type": "object"}


def test_sdk_field_raises_without_default_when_no_name_present():
    class Obj:
        pass

    try:
        sdk_field(Obj(), "input_schema", "inputSchema")
    except AttributeError as e:
        assert "input_schema" in str(e)
    else:
        raise AssertionError("expected AttributeError")


def test_sdk_field_returns_default_when_absent_and_default_given():
    class Obj:
        pass

    assert (
        sdk_field(Obj(), "structured_content", "structuredContent", default=None)
        is None
    )
    assert sdk_field(Obj(), "structured_content", "structuredContent", default={}) == {}


def test_sdk_field_present_but_none_is_not_treated_as_absent():
    """A field that legitimately holds None must be returned as None, not skipped."""

    class Obj:
        mime_type = None

    # Without a default: the attribute *exists* (value None), so this must
    # return None directly, not fall through to raising.
    assert sdk_field(Obj(), "mime_type", "mimeType") is None


# ---------------------------------------------------------------------------
# MCP protocol error surfacing
# ---------------------------------------------------------------------------


def test_describe_mcp_error_extracts_code_message_data():
    exc = _make_mcp_error(-32602, "Invalid params", data={"detail": "bad uri"})

    info = describe_mcp_error(exc)

    assert info["code"] == -32602
    assert info["message"] == "Invalid params"
    assert info["data"] == {"detail": "bad uri"}
    assert (
        "resource-not-found" in info["explanation"]
        or "Invalid params" in info["explanation"]
    )


def test_describe_mcp_error_missing_required_client_capability():
    exc = _make_mcp_error(
        -32021, "missing capability", data={"requiredCapabilities": ["elicitation"]}
    )

    info = describe_mcp_error(exc)

    assert info["code"] == -32021
    assert "MissingRequiredClientCapability" in info["explanation"]
    assert "elicitation" in info["explanation"]


def test_describe_mcp_error_unsupported_protocol_version():
    exc = _make_mcp_error(
        -32022, "unsupported version", data={"supported": ["2025-03-26", "2025-06-18"]}
    )

    info = describe_mcp_error(exc)

    assert info["code"] == -32022
    assert "UnsupportedProtocolVersion" in info["explanation"]
    assert "2025-03-26" in info["explanation"]


def test_describe_mcp_error_unknown_code_gets_generic_explanation():
    exc = _make_mcp_error(-31999, "something else")

    info = describe_mcp_error(exc)

    assert info["code"] == -31999
    assert "no specific handling registered" in info["explanation"]


def test_mcp_error_class_is_the_installed_sdks_actual_exception_type():
    exc = _make_mcp_error(-32020, "header mismatch")
    assert isinstance(exc, MCP_ERROR_CLASS)
    assert isinstance(exc, Exception)


def test_mcp_protocol_error_preserves_fields():
    err = MCPProtocolError(
        "summary text",
        code=-32021,
        message="missing capability",
        data={"requiredCapabilities": ["elicitation"]},
        explanation="MissingRequiredClientCapability: ...",
    )

    assert isinstance(err, RuntimeError)
    assert str(err) == "summary text"
    assert err.code == -32021
    assert err.message == "missing capability"
    assert err.data == {"requiredCapabilities": ["elicitation"]}
    assert "MissingRequiredClientCapability" in err.explanation


# ---------------------------------------------------------------------------
# Modern protocol version detection
# ---------------------------------------------------------------------------


def test_is_modern_protocol_version_none_is_false():
    assert is_modern_protocol_version(None) is False


def test_is_modern_protocol_version_unknown_legacy_version_is_false():
    assert is_modern_protocol_version("2025-03-26") is False


# ---------------------------------------------------------------------------
# MRTR (InputRequiredResult) unsupported-guard detection
# ---------------------------------------------------------------------------


def test_is_mrtr_unsupported_error_detects_sdk_message():
    """The bare RuntimeError mcp>=2.0 raises when a server returns
    InputRequiredResult and allow_input_required was not passed.
    """
    exc = RuntimeError(
        "Server returned InputRequiredResult; pass allow_input_required=True "
        "to receive it and retry call_tool(..., input_responses=..., "
        "request_state=result.request_state)."
    )
    assert is_mrtr_unsupported_error(exc) is True


def test_is_mrtr_unsupported_error_false_for_unrelated_runtime_error():
    assert is_mrtr_unsupported_error(RuntimeError("connection reset")) is False


def test_is_mrtr_unsupported_error_false_for_non_runtime_error():
    assert is_mrtr_unsupported_error(ValueError("InputRequiredResult")) is False


def test_mrtr_not_supported_error_is_a_runtime_error():
    err = MRTRNotSupportedError("MCP server requires interactive input (MRTR).")
    assert isinstance(err, RuntimeError)
    assert "MRTR" in str(err)
