"""Tests for MCP SDK version-compatibility helpers.

The MCP Python SDK renamed model fields from camelCase to snake_case in 2.0.0.
These tests pin the behaviour of the accessors against both attribute shapes,
plus whatever shape the currently-installed SDK actually uses.
"""

import pytest
from mcp.types import Resource, Tool

from amplifier_module_tool_mcp.sdk_compat import get_input_schema, get_mime_type

SCHEMA = {"type": "object", "properties": {"q": {"type": "string"}}}


class LegacyTool:
    """Shape exposed by mcp 1.x: camelCase attribute."""

    def __init__(self, input_schema):
        self.name = "legacy"
        self.inputSchema = input_schema  # mirrors the mcp 1.x field name


class ModernTool:
    """Shape exposed by mcp >= 2.0: snake_case attribute."""

    def __init__(self, input_schema):
        self.name = "modern"
        self.input_schema = input_schema


class SchemalessTool:
    """Shape exposed by no released SDK - neither attribute present."""

    name = "schemaless"


def test_get_input_schema_reads_legacy_camel_case_attribute():
    assert get_input_schema(LegacyTool(SCHEMA)) == SCHEMA


def test_get_input_schema_reads_modern_snake_case_attribute():
    assert get_input_schema(ModernTool(SCHEMA)) == SCHEMA


def test_get_input_schema_works_on_installed_sdk_model():
    """Guards the real regression: mcp 2.0.0 broke ``tool.inputSchema``."""
    tool = Tool(name="real", inputSchema=SCHEMA)
    assert get_input_schema(tool) == SCHEMA


def test_get_input_schema_raises_actionable_error_when_absent():
    with pytest.raises(AttributeError) as exc_info:
        get_input_schema(SchemalessTool())

    message = str(exc_info.value)
    assert "input_schema" in message
    assert "inputSchema" in message


class LegacyResource:
    """Shape exposed by mcp 1.x: camelCase attribute."""

    def __init__(self, mime_type):
        self.mimeType = mime_type  # mirrors the mcp 1.x field name


class ModernResource:
    """Shape exposed by mcp >= 2.0: snake_case attribute."""

    def __init__(self, mime_type):
        self.mime_type = mime_type


def test_get_mime_type_reads_legacy_camel_case_attribute():
    assert get_mime_type(LegacyResource("text/markdown")) == "text/markdown"


def test_get_mime_type_reads_modern_snake_case_attribute():
    assert get_mime_type(ModernResource("text/markdown")) == "text/markdown"


def test_get_mime_type_works_on_installed_sdk_model():
    resource = Resource(uri="file:///tmp/a.md", name="a", mimeType="text/markdown")
    assert get_mime_type(resource) == "text/markdown"


def test_get_mime_type_returns_none_when_sdk_model_declares_none():
    """``mimeType`` is optional in the MCP spec, so absence is not an error."""
    resource = Resource(uri="file:///tmp/a.md", name="a")
    assert get_mime_type(resource) is None


def test_get_mime_type_returns_none_when_neither_attribute_exists():
    assert get_mime_type(object()) is None
