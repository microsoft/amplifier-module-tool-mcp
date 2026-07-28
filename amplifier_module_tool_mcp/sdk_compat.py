"""Compatibility helpers for reading MCP SDK models across SDK major versions.

The MCP Python SDK renamed its ``mcp.types`` model fields from camelCase to
snake_case in 2.0.0. The JSON wire names are unchanged (they became pydantic
aliases), but *Python attribute access* differs between the two majors:

===================  ===================  ===================
Wire field           ``mcp`` 1.x          ``mcp`` >= 2.0
===================  ===================  ===================
``inputSchema``      ``.inputSchema``     ``.input_schema``
``mimeType``         ``.mimeType``        ``.mime_type``
===================  ===================  ===================

Reading a single hard-coded attribute name therefore breaks on one major or the
other. ``mcp`` 2.0.0 was published 2026-07-28; because this module declares an
unbounded ``mcp>=1.0.0`` dependency, existing installs picked it up on their
next resolve and every MCP server failed to connect with
``'Tool' object has no attribute 'inputSchema'``.

The helpers below read whichever attribute the installed SDK exposes, so the
module works unchanged on both majors.
"""

from typing import Any

__all__ = ["get_input_schema", "get_mime_type"]


def get_input_schema(tool: Any) -> dict[str, Any]:
    """Return an MCP tool's JSON input schema.

    Args:
        tool: An ``mcp.types.Tool`` (or any object exposing the schema under
            either the 1.x or 2.x attribute name).

    Returns:
        The tool's JSON Schema object.

    Raises:
        AttributeError: If neither attribute name is present. ``inputSchema`` is
            required by the MCP specification, so its absence means the SDK
            changed incompatibly again. Failing loudly beats silently
            registering a tool with no parameter schema, which would make the
            model call it incorrectly. Both transports unwrap anyio's
            ExceptionGroup (see ``errors.extract_root_cause``), so this message
            reaches the log rather than being masked.
    """
    for attr in ("input_schema", "inputSchema"):
        schema = getattr(tool, attr, None)
        if schema is not None:
            return schema

    raise AttributeError(
        f"{type(tool).__name__} exposes neither 'input_schema' (mcp >= 2.0) nor "
        "'inputSchema' (mcp 1.x). The installed MCP SDK is incompatible with "
        "this module; please report it at "
        "https://github.com/microsoft/amplifier-module-tool-mcp/issues"
    )


def get_mime_type(resource: Any) -> str | None:
    """Return an MCP resource's MIME type, or ``None`` when it declares none.

    Args:
        resource: An ``mcp.types.Resource`` (or any object exposing the MIME
            type under either the 1.x or 2.x attribute name).

    Returns:
        The declared MIME type, or ``None``. ``mimeType`` is optional in the MCP
        specification, so absence is a valid outcome rather than an error.
    """
    for attr in ("mime_type", "mimeType"):
        mime_type = getattr(resource, attr, None)
        if mime_type is not None:
            return mime_type

    return None
