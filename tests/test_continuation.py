"""Continuation regressions for duplication and ambiguous capability names."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from mcp.types import CallToolResult

from amplifier_module_tool_mcp.evidence import canonical_json, present_evidence
from amplifier_module_tool_mcp.manager import MCPManager, NamespaceCollisionError


def reconstruct(output):
    envelope = deepcopy(output["mcp_envelope"])
    for reference in output.get("mcp_envelope_references", []):
        value = output[reference["source"]]
        if "start" in reference:
            value = value[reference["start"]:reference["start"] + reference["length"]]
        parent = envelope
        for key in reference["path"][:-1]:
            parent = parent[key]
        parent[reference["path"][-1]] = value
    return envelope


@pytest.mark.parametrize("parts", [["x" * 30000], ["first\n" * 3000, "", "last-é" * 2000]])
def test_ordinary_text_deduplicates_without_recovery(parts, tmp_path):
    raw = {"content": [
        {"type": "text", "text": text, "_meta": {"index": index}}
        for index, text in enumerate(parts)
    ], "structuredContent": {"status": 403}, "_meta": {"source": "kept"}}
    result = CallToolResult.model_validate(raw)
    text = "\n".join(parts)
    output = present_evidence(result, text, {"status": 403}, 50000, tmp_path / "unused")
    assert output["evidence_complete"] and not output["content_truncated"]
    assert output["content"] == text
    assert not output.get("recovery")
    assert not (tmp_path / "unused").exists()
    assert canonical_json(reconstruct(output)) == canonical_json(
        result.model_dump(mode="json", by_alias=True, exclude_none=True)
    )
    assert len(text) + len(canonical_json(output["structured_content"])) + len(
        canonical_json(output["mcp_envelope"])
    ) + len(canonical_json(output["mcp_envelope_references"])) <= 50000


def test_ordinary_structured_deduplicates_exactly(tmp_path):
    structured = {"text": "é" * 30000, "status": 500}
    result = CallToolResult.model_validate({
        "content": [{"type": "text", "text": "summary"}],
        "structuredContent": structured, "_meta": {"source": "kept"},
    })
    output = present_evidence(result, "summary", structured, 50000, tmp_path / "unused")
    assert output["evidence_complete"]
    assert canonical_json(reconstruct(output)) == canonical_json(
        result.model_dump(mode="json", by_alias=True, exclude_none=True)
    )
    assert not output.get("recovery")


def test_json_boolean_and_number_are_not_duplicate_evidence(tmp_path):
    numeric = {"flag": 1, "pad": "p" * 400}
    envelope = {"structuredContent": {"flag": True, "pad": "p" * 400},
                "structured_content": numeric}
    output = present_evidence(envelope, "summary", numeric, 1024, tmp_path / "private")
    assert output["evidence_complete"]
    assert canonical_json(reconstruct(output)) == canonical_json(envelope)
    assert output["mcp_envelope"]["structuredContent"]["flag"] is True
    assert all(ref["path"] != ["structuredContent"] for ref in output["mcp_envelope_references"])


@pytest.mark.parametrize("names", [["a.b", "a_b"], ["duplicate", "duplicate"]])
@pytest.mark.asyncio
async def test_tool_name_collisions_are_atomic(names, mock_coordinator):
    manager = MCPManager({}, mock_coordinator)
    client = SimpleNamespace(
        get_tools=lambda: [{"name": name, "input_schema": {}} for name in names],
        get_resources=lambda: [], get_prompts=lambda: [],
    )
    with pytest.raises(NamespaceCollisionError):
        await manager._register_capabilities("same", client)
    assert manager.get_all_capabilities() == {}


@pytest.mark.asyncio
async def test_cross_server_namespace_collision_preserves_original(mock_coordinator):
    manager = MCPManager({}, mock_coordinator)
    def client(name):
        return SimpleNamespace(get_tools=lambda: [{"name": name, "input_schema": {}}],
                               get_resources=lambda: [], get_prompts=lambda: [])
    await manager._register_capabilities("a_b", client("c"))
    original = manager.tools["mcp_a_b_c"]
    with pytest.raises(NamespaceCollisionError):
        await manager._register_capabilities("a", client("b_c"))
    assert manager.tools == {"mcp_a_b_c": original}


@pytest.mark.asyncio
async def test_start_collision_fails_visibly_and_cleans_clients(mock_coordinator):
    manager = MCPManager({"servers": {"one": {"command": "unused"}}}, mock_coordinator)
    manager._start_server = AsyncMock(side_effect=NamespaceCollisionError("ambiguous"))
    manager.stop = AsyncMock()
    with pytest.raises(NamespaceCollisionError):
        await manager.start()
    manager.stop.assert_awaited_once()