"""Finite inert stdio peer. Reports only synthetic test values, never host env."""

import json
import os
import sys
import threading
from pathlib import Path

backstop = threading.Timer(15, lambda: os._exit(2))
backstop.daemon = True
backstop.start()
calls = []
for line in sys.stdin:
    request = json.loads(line)
    method = request.get("method")
    calls.append(method)
    if "id" not in request:
        continue
    response = {"jsonrpc": "2.0", "id": request["id"]}
    if method == "server/discover":
        response["error"] = {"code": -32601, "message": "legacy fixture"}
    elif method == "initialize":
        response["result"] = {
            "protocolVersion": request["params"]["protocolVersion"],
            "capabilities": {"tools": {}, "resources": {}, "prompts": {}},
            "serverInfo": {"name": "inert-host-options", "version": "1"},
        }
    elif method == "tools/list":
        page = request.get("params", {}).get("cursor")
        response["result"] = {
            "tools": [
                {
                    "name": "report" if page is None else "second",
                    "description": "Synthetic fixture values only",
                    "inputSchema": {"type": "object"},
                }
            ]
        }
        if page is None:
            response["result"]["nextCursor"] = "page-two"
    elif method in ("resources/list", "prompts/list"):
        response["result"] = {method.split("/")[0]: []}
    elif method == "tools/call":
        if request["params"]["name"] == "hang":
            continue  # Deliberate unanswered request for the finite timeout test.
        value = {
            "cwd": str(Path.cwd()),
            "sentinel": os.environ.get("MCP_TEST_SENTINEL"),
            "late": os.environ.get("MCP_TEST_LATE"),
            # This key is set to a synthetic value by the parent test.
            "term": os.environ.get("TERM"),
            "calls": calls[:],
        }
        response["result"] = {"content": [{"type": "text", "text": json.dumps(value)}]}
    else:
        response["error"] = {"code": -32601, "message": "unsupported fixture method"}
    print(json.dumps(response), flush=True)
