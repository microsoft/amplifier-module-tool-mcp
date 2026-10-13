"""MCP boundary contract checks: real Core mount -> connector -> SDK -> stdio.

Run only in an isolated ``env -i`` with HOME, TMPDIR, cwd and pytest --basetemp
inside this worktree. No provider/hosted/network composition is claimed. Server
request journals, not mocks, prove denied calls never cross the boundary.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import signal
import stat
import sys
from collections import Counter
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from amplifier_core import ModuleCoordinator

from amplifier_module_tool_mcp import mount
from amplifier_module_tool_mcp.sdk_compat import is_modern_protocol_version
from amplifier_module_tool_mcp.wrapper import MCPToolWrapper
from tests.fixtures import boundary_server as fixture

ROOT = Path(__file__).resolve().parents[1]
SERVER = Path(fixture.__file__).resolve()
ABSENT = object()
BUDGET = 2048
SAFE_ENV_KEYS = {
    "HOME", "TMPDIR", "PATH", "PYTHONPATH", "PYTHONDONTWRITEBYTECODE",
    "PYTHONNOUSERSITE", "LC_CTYPE", "PYTEST_CURRENT_TEST", "PYTEST_VERSION",
    # macOS synthesizes this during native Core imports, even under env -i.
    "__CF_USER_TEXT_ENCODING",
}
if os.name == "nt":
    # DLL/process startup and Python's Windows home resolution, not credentials.
    SAFE_ENV_KEYS.update({"SYSTEMROOT", "WINDIR", "USERPROFILE"})
pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(40)]


@pytest.fixture(autouse=True)
def isolated_environment(tmp_path: Path) -> None:
    """Refuse host-environment runs before starting a subprocess."""
    assert set(os.environ) <= SAFE_ENV_KEYS, (
        "Run with env -i; unexpected inherited environment keys: "
        f"{sorted(set(os.environ) - SAFE_ENV_KEYS)}"
    )
    for path in (Path.home(), Path(os.environ["TMPDIR"]), Path.cwd(), tmp_path):
        assert path.resolve().is_relative_to(ROOT), f"Isolation path outside worktree: {path}"
    assert not (Path.home() / ".amplifier" / "mcp.json").exists()
    assert not (Path.cwd() / ".amplifier" / "mcp.json").exists()


class BoundaryRun:
    def __init__(self, root: Path, tools: Any = ABSENT, **module_options: Any):
        self.root = root
        root.mkdir()
        self.state = root / "state.json"
        self.journal = root / "requests.jsonl"
        self.pid_file = root / "pids"
        self.state.write_text('{"revision":1}', encoding="utf-8")
        self.coordinator = ModuleCoordinator()
        self.cleanup = None
        self.closed = False
        server_config = {
            "command": sys.executable,
            "args": [
                str(SERVER), "--state", str(self.state),
                "--journal", str(self.journal), "--pid", str(self.pid_file),
            ],
        }
        if tools is not ABSENT:
            server_config["tools"] = tools
        self.config = {
            "servers": {"boundary": server_config},
            "visibility": {"enabled": False},
            "server_log_dir": str(root / "logs"),
            "artifact_dir": str(root / "artifacts"),
            "max_content_size": BUDGET,
            **module_options,
        }
        if self.config["artifact_dir"] is ABSENT:
            del self.config["artifact_dir"]

    def events(self, event: str | None = None) -> list[dict[str, Any]]:
        records = (
            [json.loads(line) for line in self.journal.read_text(encoding="utf-8").splitlines()]
            if self.journal.exists() else []
        )
        return [record for record in records if event is None or record["event"] == event]

    def requests(self, method: str, name: str | None = None) -> list[dict[str, Any]]:
        return [
            record for record in self.events("received")
            if record["method"] == method
            and (name is None or record["params"].get("name") == name)
        ]

    def executed(self, name: str) -> int:
        return sum(record.get("name") == name for record in self.events("executed"))

    @property
    def tools(self) -> dict[str, Any]:
        return self.coordinator.get("tools")

    def tool(self, name: str) -> MCPToolWrapper:
        tool = self.tools[f"mcp_boundary_{name}"]
        assert isinstance(tool, MCPToolWrapper)
        return tool

    def revise(self) -> None:
        self.state.write_text('{"revision":2}', encoding="utf-8")

    def pids(self) -> list[int]:
        return [int(line) for line in self.pid_file.read_text().splitlines()] if self.pid_file.exists() else []

    @staticmethod
    def pid_present(pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        return True

    async def close(self) -> None:
        if self.closed and not any(self.pid_present(pid) for pid in self.pids()):
            return
        self.closed = True
        try:
            if self.cleanup is not None:
                await self.cleanup()
        finally:
            # Check the kernel's process table, not a server "exited" log.
            for _ in range(100):
                if not any(self.pid_present(pid) for pid in self.pids()):
                    break
                await asyncio.sleep(0.02)
            leaked = [pid for pid in self.pids() if self.pid_present(pid)]
            # Emergency test-owned cleanup must never turn a leak into PASS.
            # Keep real client tasks drain-safe even if a broken stale wrapper
            # restarted a process AFTER manager.stop cleared its own registry.
            if leaked:
                clients = {id(tool.client): tool.client for tool in self.tools.values() if hasattr(tool, "client")}
                for client in clients.values():
                    await client.disconnect()
            for pid in leaked:
                if self.pid_present(pid):
                    os.kill(pid, signal.SIGKILL)
            for _ in range(100):
                if not any(self.pid_present(pid) for pid in self.pids()):
                    break
                await asyncio.sleep(0.02)
            received = Counter(record["method"] for record in self.events("received"))
            executed = Counter(record.get("name", record["method"]) for record in self.events("executed"))
            print(
                f"BOUNDARY {self.root.name}: received={dict(sorted(received.items()))} "
                f"executed={dict(sorted(executed.items()))} pids={self.pids()} "
                f"cleanup={'FAIL-leaked' if leaked else 'observed-absent'}",
                flush=True,
            )
            assert all(not self.pid_present(pid) for pid in self.pids()), (
                f"Server PID still present after emergency cleanup: {self.pids()}"
            )
            assert not leaked, f"Connector cleanup left live server PIDs: {leaked}"


@asynccontextmanager
async def boundary(tmp_path: Path, label: str, tools: Any = ABSENT, **options: Any):
    run = BoundaryRun(tmp_path / label, tools, **options)
    try:
        run.cleanup = await mount(run.coordinator, run.config)
        assert run.pids(), "Actual connector mount did not start the synthetic server"
        assert all(run.pid_present(pid) for pid in run.pids())
        assert len(run.events("started")) == 1
        start = run.events("started")[0]
        assert set(start["env_keys"]) <= SAFE_ENV_KEYS
        assert start["home"] == str(Path.home())
        assert start["tmpdir"] == os.environ["TMPDIR"]
        assert start["cwd"] == str(Path.cwd())
        # Select using the installed SDK's protocol helper, not an assumed major.
        modern = is_modern_protocol_version("2026-07-28")
        assert len(run.requests("server/discover" if modern else "initialize")) == 1
        assert run.requests("initialize" if modern else "server/discover") == []
        yield run
    finally:
        await run.close()


def assert_denied(run: BoundaryRun, *names: str) -> None:
    for name in names:
        assert run.requests("tools/call", name) == [], f"Denied tool reached server: {name}"
        assert run.executed(name) == 0
    print(f"DENIED {run.root.name}: names={list(names)} received=0 executed=0", flush=True)


async def test_exact_allowlist_and_current_policy_govern_real_wrappers(tmp_path: Path):
    async with boundary(tmp_path, "restricted", ["read"]) as run:
        assert set(run.tools) == {"mcp_boundary_read"}
        wrapper = run.tool("read")
        client = wrapper.client
        assert client.is_connected
        if is_modern_protocol_version("2026-07-28"):
            assert client.protocol_version == "2026-07-28"
        else:
            assert client.protocol_version == run.requests("initialize")[0]["params"]["protocolVersion"]
        assert [tool["name"] for tool in client.get_tools()] == ["read"]
        assert client.get_resources() == []
        assert client.get_prompts() == []
        arguments = {"text": "café", "nested": {"values": [None, True, 1, 1.25]}, "unknown": "kept"}
        result = await wrapper.execute(arguments)
        assert result.success, result.error
        assert result.output["structured_content"]["arguments"] == arguments
        assert run.requests("tools/call", "read")[0]["params"]["arguments"] == arguments
        assert run.executed("read") == 1

        for name in ("write", "write_more", "unknown"):
            with pytest.raises(PermissionError):
                await client.call_tool(name, {})
        with pytest.raises(PermissionError):
            await client.read_resource(fixture.RESOURCE_URI)
        with pytest.raises(PermissionError):
            await client.get_prompt(fixture.PROMPT_NAME, {})
        assert run.requests("resources/read") == []
        assert run.requests("prompts/get") == []

        # Real already-mounted wrapper must consult the client's CURRENT policy.
        policy_type = type(client.tool_policy)
        assert policy_type.__name__ == "ToolPolicy"
        client.tool_policy = policy_type(names=frozenset())
        assert client.get_tools() == []
        denied = await wrapper.execute(arguments)
        assert not denied.success
        assert denied.error
        with pytest.raises(PermissionError):
            await client.call_tool("read", arguments)
        assert len(run.requests("tools/call", "read")) == 1
        assert run.executed("read") == 1
        assert_denied(run, "write", "write_more", "unknown")
        print("DENIED current-policy read: additional_received=0 additional_executed=0", flush=True)


@pytest.mark.parametrize("tools", [ABSENT, []], ids=["absent-unrestricted", "empty-deny-all"])
async def test_absent_and_empty_policy_are_distinct(tmp_path: Path, tools: Any):
    async with boundary(tmp_path, "unrestricted" if tools is ABSENT else "deny-all", tools) as run:
        if tools is ABSENT:
            assert {f"mcp_boundary_{name}" for name in fixture.BASE_NAMES} <= set(run.tools)
            assert len(run.tools) == len(fixture.BASE_NAMES) + 2
            client = run.tool("write").client
            assert client.get_resources()
            assert client.get_prompts()
            result = await run.tool("write").execute({"unrestricted": True})
            assert result.success, result.error
            assert run.executed("write") == 1
            assert len(run.requests("tools/call", "write")) == 1
            await client.read_resource(fixture.RESOURCE_URI)
            await client.get_prompt(fixture.PROMPT_NAME, {})
            assert len(run.requests("resources/read")) == 1
            assert len(run.requests("prompts/get")) == 1
        else:
            assert run.tools == {}
            assert run.requests("tools/call") == []
            assert run.events("executed") == []
            assert_denied(run, *fixture.BASE_NAMES)


@pytest.mark.parametrize(
    "invalid",
    [None, "read", {"allow": ["read"]}, 1, True, ["read", 7], [""], ["*"], ["read*"], [" read"], ["read "]],
)
async def test_invalid_policy_fails_before_any_server_spawn(tmp_path: Path, invalid: Any):
    run = BoundaryRun(tmp_path / "invalid-policy", invalid)
    try:
        with pytest.raises(ValueError):
            run.cleanup = await mount(run.coordinator, run.config)
        assert run.pids() == []
        assert run.events() == []
        assert run.tools == {}
        print(f"INVALID tools={invalid!r}: ValueError received=0 executed=0 spawned=0", flush=True)
    finally:
        await run.close()


async def test_catalogue_and_complementary_evidence_metadata_are_inspectable(tmp_path: Path):
    async with boundary(tmp_path, "metadata", ["metadata"]) as run:
        wrapper = run.tool("metadata")
        definition = wrapper.client.get_tools()[0]
        raw_tool = next(tool for tool in fixture.catalogue(1) if tool["name"] == "metadata")
        expected_tool = fixture.sdk_tool_envelope(raw_tool)
        assert definition["output_schema"] == raw_tool["outputSchema"]
        assert definition["annotations"] == expected_tool["annotations"]
        assert definition["meta"] == fixture.CATALOG_META
        assert definition["mcp_tool_definition"] == expected_tool
        assert wrapper.mcp_tool_definition == expected_tool
        result = await wrapper.execute({})
        assert result.success, result.error
        output = result.output
        expected = fixture.sdk_result_envelope(fixture.raw_result("metadata"))
        assert output["mcp_envelope"] == expected
        assert output["content"] == expected["content"][0]["text"]
        assert output["structured_content"] == expected["structuredContent"]
        assert output["mcp_envelope"]["_meta"] == fixture.RESULT_META
        assert output["mcp_envelope"]["content"][0]["_meta"] == fixture.CONTENT_META
        assert output["mcp_envelope"]["content"][0]["annotations"]["priority"] == 0.75
        assert output["content_truncated"] is False
        assert output["evidence_complete"] is True
        assert not output.get("recovery")
        assert run.executed("metadata") == 1


@pytest.mark.parametrize("name", ["mcp_failure", "rpc_failure"])
async def test_mcp_and_protocol_errors_remain_failed_without_substitute_calls(tmp_path: Path, name: str):
    async with boundary(tmp_path, name, [name]) as run:
        result = await run.tool(name).execute({})
        assert result.success is False
        assert result.error
        if name == "mcp_failure":
            assert result.error["mcp_is_error"] is True
            assert "Synthetic MCP tool failure" in result.output["content"]
            assert result.output["structured_content"]["code"] == "SYNTHETIC_DENIAL"
            assert result.output["mcp_envelope"] == fixture.sdk_result_envelope(fixture.raw_result(name))
            assert result.output["evidence_complete"] is True
            assert run.executed(name) == 1
        else:
            assert result.error["mcp_error_code"] == -32602
            assert result.error["mcp_error_data"] == fixture.RPC_DATA
            assert "Synthetic protocol failure" in result.error["message"]
            assert run.executed(name) == 0
        assert len(run.requests("tools/call")) == 1
        assert run.requests("tools/call")[0]["params"]["name"] == name


async def test_nested_domain_statuses_are_preserved_not_classified(tmp_path: Path):
    async with boundary(tmp_path, "domain", ["domain"]) as run:
        result = await run.tool("domain").execute({})
        assert result.success is True
        assert not result.error
        assert result.output["structured_content"] == fixture.DOMAIN_STATUSES
        assert result.output["mcp_envelope"]["structuredContent"] == fixture.DOMAIN_STATUSES
        assert result.output["evidence_complete"] is True
        assert run.executed("domain") == 1


def assert_recovery(run: BoundaryRun, result: Any, name: str) -> Path:
    source = [event["envelope"] for event in run.events("result") if event["name"] == name][-1]
    expected = fixture.canonical(source)
    original_hash = hashlib.sha256(expected).hexdigest()
    print(
        f"ORIGINAL {name}: text_chars=90000 bytes={len(expected)} sha256={original_hash}",
        flush=True,
    )
    assert result.success, result.error
    output = result.output
    assert output["content_truncated"] is True
    assert output["evidence_complete"] is False
    assert output.get("mcp_envelope") is None, "Full oversized envelope leaked into presentation"
    recovery = output["recovery"]
    assert recovery["complete"] is True
    assert recovery["format"] == "mcp-sdk-envelope-json"
    assert recovery["lifetime"] == "until explicitly deleted by host"
    path = Path(recovery["path"])
    assert path.is_absolute()
    artifact_dir = Path(run.config.get("artifact_dir", Path.home() / ".amplifier/artifacts/mcp"))
    assert path.resolve().parent == artifact_dir.resolve()
    assert path.is_file()
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    # Independently constructed fixture SDK model agrees with the server log.
    assert expected == fixture.canonical(fixture.sdk_result_envelope(fixture.raw_result(name)))
    recovered = path.read_bytes()
    recovered_hash = hashlib.sha256(recovered).hexdigest()
    assert recovered == expected, "Recovery must be exact canonical SDK JSON, not a preview"
    assert recovery["bytes"] == len(expected) == len(recovered)
    assert recovery["sha256"] == original_hash == recovered_hash
    recovered_envelope = json.loads(recovered)
    assert recovered_envelope["_meta"] == fixture.RESULT_META
    assert recovered_envelope["content"][0]["_meta"] == fixture.CONTENT_META
    budget = run.config["max_content_size"]
    assert len(output["content"]) <= budget
    if name == "long_structured":
        assert output["structured_content"] is None
        assert isinstance(output["structured_preview"], str)
        assert output["structured_preview"]
        assert len(output["structured_preview"]) <= budget
        assert recovered_envelope["structuredContent"]["transcript"] == fixture.transcript()
    else:
        assert recovered_envelope["content"][0]["text"] == fixture.transcript()
    assert len(fixture.transcript()) == 90000
    # Presentation fields share the cap; fixed identity/recovery bookkeeping is
    # not charged, but duplicate source evidence is.
    presentation_size = 0
    for key in ("content", "structured_content", "structured_preview", "mcp_envelope"):
        value = output.get(key)
        if value is not None:
            presentation_size += len(value) if isinstance(value, str) else len(
                fixture.canonical(value).decode("utf-8")
            )
    assert presentation_size <= budget
    print(
        f"RECOVERY {name}: text_chars=90000 bytes={len(expected)} "
        f"original_sha256={original_hash} recovered_sha256={recovered_hash}",
        flush=True,
    )
    return path


@pytest.mark.parametrize(
    "name,budget,default_artifacts",
    [("long_text", BUDGET, False), ("long_structured", BUDGET, False), ("long_text", 1024, True)],
    ids=["text-configured", "structured-configured", "minimum-budget-default-artifacts"],
)
async def test_90k_evidence_is_bounded_and_exactly_recoverable(
    tmp_path: Path, name: str, budget: int, default_artifacts: bool,
):
    options = {"max_content_size": budget}
    if default_artifacts:
        options["artifact_dir"] = ABSENT
    async with boundary(tmp_path, name, [name], **options) as run:
        first = await run.tool(name).execute({})
        first_path = assert_recovery(run, first, name)
        original_bytes = first_path.read_bytes()
        second = await run.tool(name).execute({})
        second_path = assert_recovery(run, second, name)
        assert first_path != second_path, "Repeated calls must not overwrite an artifact"
        assert first_path.read_bytes() == original_bytes
        assert run.executed(name) == 2
        await run.close()
        # Lifetime is host-controlled: stopping the manager must not delete it.
        assert first_path.read_bytes() == original_bytes
        assert second_path.read_bytes() == original_bytes


async def test_cold_remount_refreshes_schema_reapplies_policy_and_retires_wrappers(tmp_path: Path):
    async with boundary(tmp_path, "old-mount", ["schema", "read"]) as old:
        wrapper = old.tool("schema")
        client = wrapper.client
        old_schema = json.loads(json.dumps(wrapper.input_schema))
        assert old_schema["required"] == ["value"]
        ok = await wrapper.execute({"value": "original"})
        assert ok.success, ok.error
        old.revise()
        assert wrapper.input_schema == old_schema
        invalid_args = {"value": "unchanged", "count": "not-an-int", "extra": {"nested": [None, False, 3.25]}}
        rejected = await wrapper.execute(invalid_args)
        assert not rejected.success
        assert rejected.error["mcp_error_code"] == -32602
        assert rejected.error["mcp_error_data"] == {"arguments": invalid_args, "revision": 2}
        assert old.requests("tools/call", "schema")[-1]["params"]["arguments"] == invalid_args
        assert invalid_args == {"value": "unchanged", "count": "not-an-int", "extra": {"nested": [None, False, 3.25]}}
        old_pid_count = len(old.pids())
        old_request_count = len(old.events("received"))
        await old.close()
        stale = await wrapper.execute({"value": "stale"})
        assert not stale.success
        assert stale.error
        with pytest.raises((RuntimeError, PermissionError)):
            await client.call_tool("read", {})
        assert len(old.pids()) == old_pid_count
        assert len(old.events("received")) == old_request_count
        assert all(not old.pid_present(pid) for pid in old.pids())

        # Fresh mount uses the same controlled revision, not a hot-refresh claim.
        new = BoundaryRun(tmp_path / "new-mount", ["schema", "read"])
        new.revise()
        try:
            new.cleanup = await mount(new.coordinator, new.config)
            assert new.pids()
            assert all(new.pid_present(pid) for pid in new.pids())
            assert "future_write" in new.events("catalogue")[0]["names"]
            assert set(new.tools) == {"mcp_boundary_schema", "mcp_boundary_read"}
            new_wrapper = new.tool("schema")
            assert new_wrapper.input_schema["required"] == ["count"]
            assert wrapper.input_schema == old_schema
            result = await new_wrapper.execute({"count": 17})
            assert result.success, result.error
            assert new.executed("schema") == 1
            with pytest.raises(PermissionError):
                await new_wrapper.client.call_tool("future_write", {})
            assert_denied(new, "future_write", "write", "write_more")
            print("REMOUNT: old_schema=value new_schema=count stale_received=0 stale_spawned=0", flush=True)
        finally:
            await new.close()


@pytest.mark.parametrize("invalid_budget", [0, 1023, True, "2048"])
async def test_invalid_presentation_budget_fails_before_spawn(tmp_path: Path, invalid_budget: Any):
    run = BoundaryRun(tmp_path / "invalid-budget", ["read"], max_content_size=invalid_budget)
    try:
        with pytest.raises(ValueError):
            run.cleanup = await mount(run.coordinator, run.config)
        assert run.pids() == []
        assert run.events() == []
    finally:
        await run.close()


async def test_failed_recovery_creation_is_explicit_failure(tmp_path: Path):
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("do not overwrite", encoding="utf-8")
    async with boundary(tmp_path, "artifact-failure", ["long_text"], artifact_dir=str(blocker)) as run:
        result = await run.tool("long_text").execute({})
        assert result.success is False
        assert result.error
        assert "artifact" in result.error["message"].lower() or "recovery" in result.error["message"].lower()
        if isinstance(result.output, dict):
            assert not result.output.get("recovery")
        assert blocker.read_text(encoding="utf-8") == "do not overwrite"
        assert run.executed("long_text") == 1


async def test_ordinary_30k_read_needs_no_artifact_at_default_budget(tmp_path: Path):
    # A deliberately unusable location proves ordinary reads do not need writer
    # access simply because the adapter duplicates their original text.
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("untouched", encoding="utf-8")
    async with boundary(tmp_path, "ordinary-30k", max_content_size=50000,
                        artifact_dir=str(blocker)) as run:
        result = await run.tool("ordinary_text").execute({})
        assert result.success, result.error
        output = result.output
        expected = fixture.sdk_result_envelope(fixture.raw_result("ordinary_text"))
        assert len(expected["content"][0]["text"]) == 30000
        assert output["content"] == expected["content"][0]["text"]
        assert output["evidence_complete"] and not output["content_truncated"]
        assert not output.get("recovery")
        # Rebuild the SDK envelope from explicit adapter references.
        envelope = json.loads(json.dumps(output["mcp_envelope"]))
        for reference in output["mcp_envelope_references"]:
            value = output[reference["source"]]
            if "start" in reference:
                value = value[reference["start"]:reference["start"] + reference["length"]]
            parent = envelope
            for key in reference["path"][:-1]:
                parent = parent[key]
            parent[reference["path"][-1]] = value
        assert fixture.canonical(envelope) == fixture.canonical(expected)
        assert blocker.read_text(encoding="utf-8") == "untouched"
        assert run.executed("ordinary_text") == 1
        print("ORDINARY 30000 chars: complete=True recovery=None exact_reconstruction=True", flush=True)