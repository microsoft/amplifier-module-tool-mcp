"""Compatibility helpers for bridging `mcp` SDK majors (1.x <-> 2.x).

The `mcp` SDK renamed several Pydantic model fields from camelCase to
snake_case between the 1.x and 2.x major releases (e.g. ``Tool.inputSchema``
-> ``Tool.input_schema``, ``Resource.mimeType`` -> ``Resource.mime_type``),
renamed its protocol-error exception class (``McpError`` -> ``MCPError``),
and -- most significantly -- introduced a new connect-time negotiation
entry point (``negotiate_auto``) for the stateless 2026-07-28 protocol that
does not exist at all in 1.x. This module lets callers work with either SDK
major without scattering ``hasattr``/``try``/``except ImportError`` checks
through the rest of the codebase.

Design constraint -- fail loud, no silent fallbacks:

A prior version of this code used patterns like::

    resource.mimeType if hasattr(resource, "mimeType") else None

On the SDK version where the field was renamed, ``hasattr`` evaluates to
``False`` and the expression silently returns ``None`` -- even though the
object legitimately carries the data under its new name. That is data loss
disguised as a missing-value case, and it produces no error, log, or any
other signal that something is wrong. The bug this module fixes was exactly
that pattern.

``sdk_field`` instead:

- Returns the value under the FIRST candidate name that exists on the
  object, using ``hasattr`` (not truthiness) to decide "exists" -- so a
  field that legitimately holds ``None`` (e.g. a resource with no MIME
  type) is still returned as ``None``, not treated as absent.
- Raises ``AttributeError`` when NONE of the candidate names exist and no
  ``default`` was supplied. An object that doesn't have any of the known
  names is an unrecognized SDK shape, not a missing-value case, and must
  announce itself loudly rather than being papered over with a default --
  UNLESS the caller explicitly opts in via ``default=`` because the field
  is genuinely optional in the protocol itself (e.g. ``structuredContent``),
  not merely renamed.

Every other helper in this module follows the same discipline: a *known*
older SDK takes a *deliberate, logged* legacy path; an *unrecognized* SDK
shape raises instead of guessing.
"""

from __future__ import annotations

import logging
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from typing import Any

logger = logging.getLogger(__name__)

# Sentinel distinguishing "no default supplied" from a legitimate default
# value of ``None`` (which callers must be able to pass explicitly).
_MISSING = object()


def sdk_field(obj: Any, *names: str, default: Any = _MISSING) -> Any:
    """Read a field from ``obj`` that the `mcp` SDK has renamed across majors.

    Tries each name in ``names`` in order and returns the value of the first
    one that exists on ``obj`` (existence checked via ``hasattr``, so a
    present-but-``None`` value is returned as-is, not skipped).

    Args:
        obj: The SDK model instance to read from (e.g. a ``Tool``,
            ``Resource``, or ``ResourceTemplate`` instance).
        *names: Candidate attribute names to try, in priority order (e.g.
            ``"input_schema", "inputSchema"``).
        default: Value to return if NONE of ``names`` exist on ``obj``.
            Omit this (the default) to keep the fail-loud behavior for
            fields that must exist under one name or another -- e.g. a
            renamed-but-always-present field. Pass ``default=`` only for
            fields that are genuinely optional in the protocol itself
            (e.g. ``structuredContent``/``structured_content``, which a
            server may simply not return), where absence is a legitimate
            state rather than an unrecognized SDK shape.

    Returns:
        The value of the first attribute name that exists on ``obj``. May
        legitimately be ``None`` if that attribute's value is ``None``.

    Raises:
        AttributeError: If none of ``names`` exist on ``obj`` and no
            ``default`` was supplied. Names at least one name that was
            attempted, and the object's type, so the failure is
            self-describing.

    Example:
        >>> sdk_field(tool, "input_schema", "inputSchema")
        {'type': 'object', 'properties': {...}}
        >>> sdk_field(result, "structured_content", "structuredContent", default=None)
        None
    """
    if not names:
        raise ValueError("sdk_field() requires at least one candidate name")

    for name in names:
        if hasattr(obj, name):
            return getattr(obj, name)

    if default is not _MISSING:
        return default

    raise AttributeError(
        f"{type(obj).__name__!r} object has none of the expected attributes: "
        f"{', '.join(names)}. This likely means the installed `mcp` SDK "
        f"version uses a field shape this module does not yet know about."
    )


def extract_root_cause(exc: BaseException) -> BaseException:
    """Unwrap ExceptionGroup to the most informative root cause.

    The MCP SDK's anyio TaskGroup surfaces transport errors as
    ExceptionGroup("unhandled errors in a TaskGroup", [actual_error]).
    Unwrapping gives callers the real cause (e.g. HTTPStatusError 502)
    instead of the opaque ExceptionGroup string.

    Available natively from Python 3.11; anyio produces ExceptionGroup
    on earlier versions too via the exceptiongroup backport.
    """
    if isinstance(exc, ExceptionGroup) and exc.exceptions:
        return extract_root_cause(exc.exceptions[0])
    return exc


# ---------------------------------------------------------------------------
# Protocol negotiation (2026-07-28 stateless handshake vs legacy `initialize`)
# ---------------------------------------------------------------------------
#
# `mcp` 2.x introduced `negotiate_auto()`, which probes `server/discover` at
# the latest modern protocol version and, on any non-modern evidence, falls
# back to `initialize()` itself -- this IS the spec's era-detection state
# machine, already implemented by the SDK. `mcp` 1.x has no such symbol; the
# only handshake it knows how to do is the legacy `initialize()` call, which
# never populates `_meta` and never sends `server/discover`.
#
# `negotiate()` below selects between these based on whether the symbol is
# importable -- NOT based on catching an exception from calling it. Catching
# broadly and falling back silently would hide the real failure mode this
# module exists to prevent: if `negotiate_auto` exists but raises, that is a
# genuine incompatibility with a modern-only server and must propagate, not
# be papered over by retrying legacy (the SDK already retries legacy
# internally for every case where that is the correct response).

_legacy_negotiation_warned = False


def _warn_legacy_negotiation_once(server_name: str) -> None:
    """Log once per process that this connection is using the legacy handshake.

    This is a real, visible protocol degradation (no `server/discover`, no
    `_meta` on requests, a 2025-03-26-era wire format) caused by an installed
    `mcp` SDK that predates `negotiate_auto` (i.e. `mcp<2.0`). Logged at
    WARNING exactly once per process -- not INFO -- because Python's
    effective default log level with no configuration is WARNING: a host
    app that never explicitly raises its log level would otherwise never
    see this message at all, and would silently run a handshake-era
    protocol forever without any visible signal that it's degraded.
    """
    global _legacy_negotiation_warned
    if _legacy_negotiation_warned:
        return
    _legacy_negotiation_warned = True
    logger.warning(
        f"MCP server '{server_name}': installed `mcp` SDK predates modern "
        f"protocol negotiation (`negotiate_auto` is not importable from "
        f"mcp.client.client -- this is expected for mcp<2.0). Falling back "
        f"to the legacy `initialize()` handshake; this connection (and all "
        f"others in this process) will speak a pre-2026-07-28, "
        f"handshake-era protocol. Upgrade the installed `mcp` package to "
        f"2.x to negotiate the modern stateless protocol instead."
    )


async def negotiate(session: Any, *, server_name: str = "") -> str | None:
    """Negotiate the MCP protocol version to use for ``session``.

    On `mcp>=2.0` (where `mcp.client.client.negotiate_auto` exists), this
    drives the SDK's full connect-time policy: it probes `server/discover`
    at the latest modern (2026-07-28+) version and, on any non-modern
    evidence, falls back to `initialize()` itself. No `initialize` /
    `notifications/initialized` traffic is sent on the modern path; every
    subsequent request carries full `_meta` automatically.

    On `mcp<2.0` (no `negotiate_auto`), falls back to the only handshake
    that SDK major knows: `await session.initialize()`. This is a real,
    visible protocol downgrade -- logged at INFO once per process via
    `_warn_legacy_negotiation_once` -- not a silently-chosen default.

    Args:
        session: An unconnected/un-negotiated `ClientSession`.
        server_name: Used only for the legacy-path log message.

    Returns:
        The negotiated protocol version string (e.g. ``"2026-07-28"`` or a
        legacy handshake version like ``"2025-03-26"``), or ``None`` if it
        could not be determined from the SDK's own result objects.

    Raises:
        Exception: Whatever `negotiate_auto` (modern path) or
            `session.initialize()` (legacy path) raises. A modern-only
            server this client cannot talk to is a real error, not a
            reason to retry -- `negotiate_auto` already retries legacy
            internally for every case where that's the correct response.
    """
    import importlib

    try:
        _client_mod = importlib.import_module("mcp.client.client")
    except ImportError:
        _client_mod = None

    if _client_mod is None:
        _warn_legacy_negotiation_once(server_name)
        result = await session.initialize()
        return sdk_field(result, "protocol_version", "protocolVersion", default=None)

    # `mcp.client.client` importing successfully means `negotiate_auto` must
    # exist -- it's the module this SDK ships specifically to provide it. A
    # missing attribute here is an unrecognized 2.x-line shape, so let the
    # natural AttributeError from getattr() surface rather than guessing.
    negotiate_auto = _client_mod.negotiate_auto
    await negotiate_auto(session)
    # mcp>=2.0's ClientSession exposes the negotiated version directly once
    # `negotiate_auto` (or `initialize`/`adopt`) has run. If `negotiate_auto`
    # was importable but this attribute is missing, that's an unrecognized
    # SDK shape within the 2.x line -- let the natural AttributeError surface
    # rather than guessing.
    return session.protocol_version


# Modern (2026-07-28+) protocol versions, mirrored from `mcp.types.version`
# on SDKs that define it. `mcp<2.0` has no concept of "modern" versions at
# all -- every version it knows is a legacy handshake version -- so the
# import failing is expected and correctly yields an empty tuple, not an
# error.
def _load_modern_protocol_versions() -> tuple[str, ...]:
    import importlib

    try:
        version_mod = importlib.import_module("mcp.types.version")
    except ImportError:
        return ()
    return tuple(version_mod.MODERN_PROTOCOL_VERSIONS)


_MODERN_PROTOCOL_VERSIONS = _load_modern_protocol_versions()


def is_modern_protocol_version(version: str | None) -> bool:
    """Return True if ``version`` is a 2026-07-28-era stateless protocol version.

    Always False for ``None`` (not yet negotiated) and for any version
    string when the installed SDK is `mcp<2.0` (which has no modern
    versions to negotiate in the first place).
    """
    return version is not None and version in _MODERN_PROTOCOL_VERSIONS


def supports_log_level_kwarg() -> bool:
    """Whether the installed `mcp` SDK's `ClientSession.__init__` accepts `log_level`.

    `mcp>=2.0` added a `log_level` keyword so log level can be negotiated
    once at session construction (replacing the removed `logging/setLevel`
    RPC, which is now expressed per-request via `_meta` instead). `mcp<2.0`
    has no such parameter. Checked via `inspect.signature` rather than a
    hardcoded version comparison, so this stays correct if the parameter is
    ever added to (or removed from) a specific point release.
    """
    import inspect

    from mcp import ClientSession

    return "log_level" in inspect.signature(ClientSession.__init__).parameters


def build_client_info() -> Any:
    """Build a `types.Implementation` identifying this module to MCP servers.

    Prior to this, every connection sent the SDK's own placeholder identity
    (``{"name": "mcp", "version": "0.1.0"}``) instead of identifying this
    module. This reads the real installed package version via
    `importlib.metadata` (falling back to the package's own `__version__`
    if the metadata isn't available, e.g. an unusual install layout) rather
    than hardcoding a version literal that would drift out of sync with
    `pyproject.toml`.

    Only fields common to both `mcp` 1.x and 2.x `Implementation` models
    (``name``, ``version``) are set explicitly -- 1.x uses ``websiteUrl``
    where 2.x uses ``description``/``website_url``, so passing either would
    break the other major.
    """
    from mcp import types

    try:
        pkg_version = _pkg_version("amplifier-module-tool-mcp")
    except PackageNotFoundError:
        from amplifier_module_tool_mcp import __version__ as pkg_version

    return types.Implementation(name="amplifier-module-tool-mcp", version=pkg_version)


# ---------------------------------------------------------------------------
# MCP protocol error surfacing
# ---------------------------------------------------------------------------
#
# `mcp` renamed its protocol-error exception class from `McpError` (1.x) to
# `MCPError` (2.x). Both, however, store the same `ErrorData` (with `code`,
# `message`, `data` fields) under a `.error` attribute -- so once the correct
# exception class is selected for the installed SDK, `.error.code` /
# `.error.message` / `.error.data` is a uniform read across both majors.


def _load_mcp_error_class() -> type[Exception]:
    import mcp.shared.exceptions as _exceptions_mod

    # `mcp.shared.exceptions` itself exists on both majors -- only the class
    # name inside it differs -- so this is a plain (non-conditional) import,
    # and the name lookup is done dynamically via getattr to try both names
    # without pyright flagging a nonexistent symbol for whichever major isn't
    # installed.
    cls = getattr(_exceptions_mod, "MCPError", None) or getattr(
        _exceptions_mod, "McpError", None
    )
    if cls is None:
        raise ImportError(
            "Neither `MCPError` (mcp>=2.0) nor `McpError` (mcp<2.0) exists "
            "in mcp.shared.exceptions. This is an unrecognized `mcp` SDK "
            "shape that amplifier_module_tool_mcp.sdk_compat does not know "
            "how to handle -- refusing to guess."
        )
    return cls


#: The installed SDK's protocol-error exception class (`McpError` on 1.x,
#: `MCPError` on 2.x). Use this for `isinstance` checks instead of importing
#: either name directly, so callers work on both majors.
MCP_ERROR_CLASS: type[Exception] = _load_mcp_error_class()

# JSON-RPC error codes defined or carried forward by the 2026-07-28 spec.
# Codes not in this table are still handled (via `describe_mcp_error`'s
# generic fallback) -- this table only supplies the human-readable
# explanation for the well-known ones.
MCP_ERROR_DESCRIPTIONS: dict[int, str] = {
    -32020: (
        "HeaderMismatch: the HTTP 'MCP-Protocol-Version' header did not "
        "match the protocol version declared in the request body's _meta."
    ),
    -32021: (
        "MissingRequiredClientCapability: the server requires a client "
        "capability this client did not declare in _meta.clientCapabilities."
    ),
    -32022: (
        "UnsupportedProtocolVersion: the server does not support any "
        "protocol version this client offered."
    ),
    -32602: (
        "Invalid params. As of 2026-07-28 this code is also used for "
        "resource-not-found (moved from -32002)."
    ),
    -32002: (
        "Resource not found (legacy code, superseded by -32602 in "
        "2026-07-28; clients SHOULD still accept it from older servers)."
    ),
}


def describe_mcp_error(exc: BaseException) -> dict[str, Any]:
    """Extract a structured, human-readable description from an MCP protocol error.

    Works uniformly across `mcp` 1.x (`McpError(error: ErrorData)`) and 2.x
    (`MCPError(code, message, data=None)`) because both expose the
    server's error under `.error` (an `ErrorData` with `.code`/`.message`/
    `.data`) -- see the module-level comment above `MCP_ERROR_CLASS`.

    Args:
        exc: An instance of `MCP_ERROR_CLASS` (caller should `isinstance`-check
            against `MCP_ERROR_CLASS` before calling this).

    Returns:
        Dict with keys `code`, `message`, `data`, and `explanation` (from
        `MCP_ERROR_DESCRIPTIONS`, augmented with `data` details for
        `-32021`/`-32022`, or a generic note for unlisted codes).
    """
    error_data = sdk_field(exc, "error")
    code = sdk_field(error_data, "code")
    message = sdk_field(error_data, "message")
    data = sdk_field(error_data, "data")

    explanation = MCP_ERROR_DESCRIPTIONS.get(
        code,
        f"MCP protocol error {code} (no specific handling registered for this code).",
    )
    if code == -32021 and isinstance(data, dict) and "requiredCapabilities" in data:
        explanation = (
            f"{explanation} Missing capabilities: {data['requiredCapabilities']!r}."
        )
    elif code == -32022 and isinstance(data, dict) and "supported" in data:
        explanation = f"{explanation} Server supports: {data['supported']!r}."

    return {"code": code, "message": message, "data": data, "explanation": explanation}


class MCPProtocolError(RuntimeError):
    """Raised when an MCP server returns a well-formed JSON-RPC protocol error.

    Distinguishes protocol-level failures (a JSON-RPC error response with a
    real error code -- the server understood the request and rejected it)
    from transport-level failures (connection refused, timeout, malformed
    response), which continue to raise plain `RuntimeError` as before.

    Attributes:
        code: The JSON-RPC error code (e.g. -32602, -32022).
        message: The server's error message.
        data: The server's error `data` payload, if any (e.g.
            `{"requiredCapabilities": [...]}` for -32021).
        explanation: Human-readable explanation of the code, from
            `MCP_ERROR_DESCRIPTIONS`.
    """

    def __init__(
        self, summary: str, *, code: int, message: str, data: Any, explanation: str
    ) -> None:
        super().__init__(summary)
        self.code = code
        self.message = message
        self.data = data
        self.explanation = explanation


# ---------------------------------------------------------------------------
# MRTR (Multi Round-Trip Requests) -- unsupported, but must fail honestly
# ---------------------------------------------------------------------------
#
# `mcp>=2.0` added MRTR: a server may answer `tools/call` / `prompts/get` /
# `resources/read` with `InputRequiredResult` instead of the normal result,
# asking the client to supply sampling/elicitation/roots input and retry.
# Wiring MRTR end-to-end is a real design effort (see docs/GAP_ANALYSIS.md
# §5.2) -- there is no established pattern yet for how an Amplifier agent
# produces that answer. This module does not attempt that design here; it
# only makes the *failure* honest.
#
# Without this, `ClientSession.call_tool` (etc.) raises a bare `RuntimeError`
# instructing the caller to "pass allow_input_required=True ... and retry
# ...(..., input_responses=..., request_state=...)". That message reaches
# the calling Amplifier agent verbatim via wrapper.py's generic exception
# handler -- an LLM agent reading it has no way to act on it, because this
# module exposes no `allow_input_required` / `input_responses` parameter
# anywhere in its public surface. Telling a caller to do something the
# module doesn't let it do is worse than a plain failure.


def is_mrtr_unsupported_error(exc: BaseException) -> bool:
    """True if ``exc`` is the SDK's built-in guard against an unanswered MRTR response.

    On `mcp>=2.0`, `ClientSession.call_tool` / `get_prompt` / `read_resource`
    raise a bare `RuntimeError` (no dedicated exception subclass -- see
    `mcp.client.session._input_required_unexpected`) whenever the server
    returns `InputRequiredResult` and the caller did not pass
    `allow_input_required=True`. This module never passes that flag (MRTR
    is not wired -- see docs/GAP_ANALYSIS.md §5.2), so any occurrence of
    this error means a server tried to elicit input this client cannot
    supply.

    Detection is necessarily message-based: the installed SDK exposes no
    dedicated exception type for this specific case (contrast with
    `InputRequiredRoundsExceededError`, which covers a different MRTR
    failure -- exceeding the retry-round cap of a driver this module
    never invokes). `mcp<2.0` has no `InputRequiredResult` concept at all,
    so this can only ever be True against `mcp>=2.0`.
    """
    return isinstance(exc, RuntimeError) and "InputRequiredResult" in str(exc)


class MRTRNotSupportedError(RuntimeError):
    """Raised in place of the SDK's un-actionable MRTR guard message.

    The server requested multi-round-trip input (MRTR / `InputRequiredResult`)
    to complete the call, and this module does not yet implement a way to
    answer that request. Callers should treat this the same as any other
    tool-execution failure -- there is no retry that will succeed.
    """
