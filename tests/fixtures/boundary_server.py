"""Controlled local JSON-line MCP server; never imports connector code.

The client side uses the real MCP SDK transport. This fixture intentionally
implements only the methods needed by the boundary tests, with an independent
request/execution journal and deterministic SDK-envelope oracle.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from mcp import types

RESOURCE_URI = "boundary://evidence"
PROMPT_NAME = "boundary_prompt"
BASE_NAMES = (
    "read",
    "write",
    "write_more",
    "schema",
    "metadata",
    "domain",
    "mcp_failure",
    "rpc_failure",
    "long_text",
    "long_structured",
    "ordinary_text",
)
CATALOG_META = {"fixture": "boundary", "source": {"revision": "synthetic"}}
RESULT_META = {"request_ref": "synthetic-42", "provenance": {"source": "fixture"}}
CONTENT_META = {"source_ref": "cue-17", "confidence": 0.75}
DOMAIN_STATUSES = {
    "outer": {"status": 403, "error": "domain-owned"},
    "results": [{"status": "failed", "code": "AccessDenied"}, {"status": 500}],
}
RPC_DATA = {"reason": "synthetic rejection", "details": {"retry": False}}


def canonical(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def sdk_result_envelope(raw: dict[str, Any]) -> dict[str, Any]:
    """Independent oracle: serialize the SDK model, not an adapter output."""
    return types.CallToolResult.model_validate(raw).model_dump(
        mode="json", by_alias=True, exclude_none=True
    )


def sdk_tool_envelope(raw: dict[str, Any]) -> dict[str, Any]:
    return types.Tool.model_validate(raw).model_dump(
        mode="json", by_alias=True, exclude_none=True
    )


def transcript() -> str:
    prefix = "WEBVTT\n\n"
    cue = "00:00:01.000 --> 00:00:02.000\nSynthetic café evidence.\n\n"
    tail = "\nEND-EXACT-TEXT-é\n"
    return prefix + (cue * 2000)[: 90000 - len(prefix) - len(tail)] + tail


def catalogue(revision: int) -> list[dict[str, Any]]:
    names = BASE_NAMES + (("future_write",) if revision >= 2 else ())
    tools = []
    for name in names:
        schema: dict[str, Any] = {"type": "object", "additionalProperties": True}
        if name == "schema":
            field, kind = ("value", "string") if revision == 1 else ("count", "integer")
            schema = {
                "type": "object",
                "properties": {field: {"type": kind}},
                "required": [field],
                "additionalProperties": False,
            }
        tools.append(
            {
                "name": name,
                "title": f"Synthetic {name}",
                "description": f"Controlled fixture tool {name}",
                "inputSchema": schema,
                "outputSchema": {"type": "object"},
                "annotations": {
                    "title": "Evidence hint, not permission",
                    "readOnlyHint": True,
                    "destructiveHint": False,
                    "idempotentHint": True,
                    "openWorldHint": False,
                },
                "_meta": CATALOG_META,
            }
        )
    return tools


def raw_result(name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    text = f"executed {name}"
    structured: dict[str, Any] | None = {"arguments": arguments or {}, "tool": name}
    is_error = False
    if name == "metadata":
        text, structured = "Complementary source text: café", {"answer": 42, "source": "cue-17"}
    elif name == "domain":
        text, structured = "Domain statuses are evidence", DOMAIN_STATUSES
    elif name == "mcp_failure":
        text, structured, is_error = (
            "Synthetic MCP tool failure",
            {"code": "SYNTHETIC_DENIAL", "context": {"source": "fixture"}},
            True,
        )
    elif name == "long_text":
        text, structured = transcript(), {"source": "WebVTT", "characters": 90000}
    elif name == "long_structured":
        text = "Structured transcript follows"
        structured = {"transcript": transcript(), "statuses": DOMAIN_STATUSES, "tail": "EXACT-é"}
    elif name == "ordinary_text":
        text, structured = "ordinary café\n" * 2000 + "x" * 2000, {"kind": "ordinary"}
    return {
        "content": [
            {
                "type": "text",
                "text": text,
                "annotations": {"audience": ["user"], "priority": 0.75},
                "_meta": CONTENT_META,
            }
        ],
        "structuredContent": structured,
        "isError": is_error,
        "_meta": RESULT_META,
        "resultType": "complete",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--journal", type=Path, required=True)
    parser.add_argument("--pid", type=Path, required=True)
    options = parser.parse_args()

    def record(event: str, **fields: Any) -> None:
        with options.journal.open("a", encoding="utf-8") as stream:
            stream.write(canonical({"event": event, **fields}).decode("utf-8") + "\n")

    with options.pid.open("a", encoding="ascii") as stream:
        stream.write(f"{os.getpid()}\n")
    record(
        "started",
        pid=os.getpid(),
        env_keys=sorted(os.environ),
        home=os.environ.get("HOME"),
        tmpdir=os.environ.get("TMPDIR"),
        cwd=os.getcwd(),
    )
    try:
        for line in sys.stdin:
            request = json.loads(line)
            method = request["method"]
            params = request.get("params") or {}
            record("received", method=method, params=params, id=request.get("id"))
            if "id" not in request:
                continue
            state = json.loads(options.state.read_text(encoding="utf-8"))
            revision = state["revision"]
            if state.get("pause_startup") and method in {"server/discover", "initialize"}:
                record("startup_paused", method=method)
                continue  # Keep the real SDK connection waiting for cancellation.
            error = None
            if method == "server/discover":
                result = {
                    "supportedVersions": ["2026-07-28"],
                    "capabilities": {"tools": {}, "resources": {}, "prompts": {}},
                    "ttlMs": 0, "cacheScope": "private", "resultType": "complete",
                }
            elif method == "initialize":
                result = {
                    "protocolVersion": params["protocolVersion"],
                    "capabilities": {"tools": {}, "resources": {}, "prompts": {}},
                    "serverInfo": {"name": "boundary-fixture", "version": "1"},
                }
            elif method == "tools/list":
                result = {
                    "tools": catalogue(revision), "ttlMs": 0,
                    "cacheScope": "private", "resultType": "complete",
                }
                if state.get("invalid_input_schema"):
                    result["tools"][-1]["inputSchema"] = []
                record("catalogue", revision=revision, names=[t["name"] for t in result["tools"]])
            elif method == "resources/list":
                result = {
                    "resources": [{"uri": RESOURCE_URI, "name": "evidence", "mimeType": "text/plain"}],
                    "ttlMs": 0, "cacheScope": "private", "resultType": "complete",
                }
            elif method == "prompts/list":
                result = {
                    "prompts": [{"name": PROMPT_NAME, "description": "Synthetic prompt"}],
                    "ttlMs": 0, "cacheScope": "private", "resultType": "complete",
                }
            elif method == "resources/read":
                record("executed", method=method)
                result = {
                    "contents": [{"uri": RESOURCE_URI, "text": "resource evidence"}],
                    "ttlMs": 0, "cacheScope": "private", "resultType": "complete",
                }
            elif method == "prompts/get":
                record("executed", method=method)
                result = {
                    "messages": [{"role": "user", "content": {"type": "text", "text": "prompt evidence"}}],
                    "resultType": "complete",
                }
            elif method == "tools/call":
                name, arguments = params["name"], params.get("arguments", {})
                if name not in {t["name"] for t in catalogue(revision)}:
                    error = {"code": -32602, "message": "Unknown synthetic tool", "data": {"name": name}}
                elif name == "rpc_failure":
                    error = {"code": -32602, "message": "Synthetic protocol failure", "data": RPC_DATA}
                elif name == "schema" and not (
                    (revision == 1 and set(arguments) == {"value"} and isinstance(arguments["value"], str))
                    or (revision >= 2 and set(arguments) == {"count"} and type(arguments["count"]) is int)
                ):
                    error = {
                        "code": -32602,
                        "message": "Synthetic schema mismatch: arguments unchanged",
                        "data": {"arguments": arguments, "revision": revision},
                    }
                else:
                    record("executed", method=method, name=name, arguments=arguments)
                    result = raw_result(name, arguments)
                    # Expected bytes originate at the source, independently of
                    # the connector's preview or artifact implementation.
                    record("result", name=name, envelope=sdk_result_envelope(result))
            elif method == "ping":
                result = {}
            else:
                error = {"code": -32601, "message": f"Unsupported fixture method {method}"}
            response = {"jsonrpc": "2.0", "id": request["id"]}
            response["error" if error is not None else "result"] = error if error is not None else result
            print(canonical(response).decode("utf-8"), flush=True)
    finally:
        record("exited", pid=os.getpid())


if __name__ == "__main__":
    main()