"""Actual SDK queue, HTTP construction/auth hook and finite local transport.

No model/server account or real network. Final installed-path qualification is
separate from these forced queue/cancellation/credential controls.
"""

import asyncio
import json
from importlib.metadata import version
from types import SimpleNamespace

import pytest
import pytest_asyncio

import mcp.client.streamable_http as sdk_http
from mcp.client.streamable_http import StreamableHTTPTransport
from amplifier_module_tool_mcp import streamable_http_client as product
from amplifier_module_tool_mcp.sdk_compat import MCPProtocolError

SDK_MAJOR = int(version("mcp").split(".")[0])
httpx = sdk_http.httpx if SDK_MAJOR == 1 else sdk_http.httpx2
pytestmark = pytest.mark.timeout(10)


def rpc_method(ctx):
    message = ctx.session_message.message
    return (message.root if SDK_MAJOR == 1 else message).method


@pytest_asyncio.fixture(params=["legacy", "modern"] if SDK_MAJOR >= 2 else ["legacy"])
async def connected(monkeypatch, request):
    effects = []
    state = SimpleNamespace(
        redirect=False, redirect_status=307, unexpected_gets=[], auth_posts=[]
    )
    modern = request.param == "modern"

    async def peer(request):
        if request.url.path == "/oauth/token":
            state.auth_posts.append(request.content)
            return httpx.Response(200, json={"access_token": "finite-auth-result"})
        if request.method != "POST":
            state.unexpected_gets.append(str(request.url))
            return httpx.Response(405)
        body = json.loads(request.content)
        method = body.get("method")
        if "id" not in body:
            return httpx.Response(202)
        if method == "server/discover":
            if modern:
                return httpx.Response(
                    200,
                    json={
                        "jsonrpc": "2.0",
                        "id": body["id"],
                        "result": {
                            "supportedVersions": ["2026-07-28"],
                            "capabilities": {
                                "tools": {},
                                "resources": {},
                                "prompts": {},
                            },
                        },
                    },
                )
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "error": {"code": -32601, "message": "Use legacy initialize"},
                },
            )
        if method == "initialize":
            result = {
                "protocolVersion": body["params"]["protocolVersion"],
                "capabilities": {"tools": {}, "resources": {}, "prompts": {}},
                "serverInfo": {"name": "finite-fixture", "version": "1"},
            }
        elif method == "tools/list":
            result = {
                "tools": [
                    {
                        "name": "write",
                        "description": "Finite fixture effect",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "value": {"type": "integer"},
                                "tenant": {"type": "string", "x-mcp-header": "Tenant"},
                            },
                            "required": ["value"],
                        },
                    }
                ]
            }
        elif method in ("resources/list", "prompts/list"):
            result = {method.split("/")[0]: []}
        elif method == "resources/read":
            return httpx.Response(
                200,
                headers={"set-cookie": "authority=response-B; Path=/"},
                json={
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "result": {
                        "contents": [],
                        "cacheScope": "private",
                        "ttlMs": 0,
                        "resultType": "complete",
                    },
                },
            )
        elif method == "tools/call":
            if state.redirect and request.url.path == "/mcp":
                return httpx.Response(
                    state.redirect_status, headers={"location": "/mcp-target"}
                )
            effects.append(
                {
                    "params": body["params"],
                    "headers": dict(request.headers),
                    "url": str(request.url),
                }
            )
            result = {"content": [{"type": "text", "text": "done"}], "isError": False}
        else:
            raise AssertionError(f"Unexpected request {method}")
        if modern:
            result.update(cacheScope="private", ttlMs=0, resultType="complete")
        return httpx.Response(
            200, json={"jsonrpc": "2.0", "id": body["id"], "result": result}
        )

    http = httpx.AsyncClient(
        transport=httpx.MockTransport(peer),
        headers={"authorization": "Bearer fixture-A"},
        timeout=2,
        follow_redirects=True,
    )
    monkeypatch.setattr(product, "create_mcp_http_client", lambda **kwargs: http)
    client = product.MCPStreamableHTTPClient(
        "fixture",
        "http://fixture.test/mcp",
        headers={"authorization": "Bearer fixture-A"},
    )
    await asyncio.wait_for(client.connect(), 3)
    try:
        yield SimpleNamespace(
            client=client, http=http, effects=effects, modern=modern, state=state
        )
    finally:
        await asyncio.wait_for(client.disconnect(), 3)
    assert not client._approval_http


@pytest.mark.asyncio
async def test_actual_sdk_scoped_send_and_once_without_policy(connected):
    c = connected.client
    binding = c.approval_binding()
    assert binding["httpSendAuthorization"] == 1
    assert "fixture-A" not in repr(binding)
    with c.approval_scope(binding, "write", {"value": 1}):
        await c.call_tool("write", {"value": 1})
        with pytest.raises(ValueError, match="changed"):
            await c.call_tool("write", {"value": 1})
    await c.call_tool("write", {"value": 2})
    assert [e["params"]["arguments"] for e in connected.effects] == [
        {"value": 1},
        {"value": 2},
    ]
    assert not c._approval_http.pending


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change", ["cookie", "header", "auth", "endpoint", "session", "retired"]
)
async def test_actual_sdk_queue_drift_refuses_before_http_effect(
    connected, monkeypatch, change
):
    c, http = connected.client, connected.http
    entered, release = asyncio.Event(), asyncio.Event()
    original = StreamableHTTPTransport._handle_post_request

    async def delayed(self, ctx):
        if rpc_method(ctx) == "tools/call":
            entered.set()
            await release.wait()
        return await original(self, ctx)

    monkeypatch.setattr(StreamableHTTPTransport, "_handle_post_request", delayed)
    old_session = c.session
    with c.approval_scope(c.approval_binding(), "write", {"value": 1}):
        task = asyncio.create_task(c.call_tool("write", {"value": 1}))
        await asyncio.wait_for(entered.wait(), 2)
        if change == "cookie":
            http.cookies.set("authority", "B", domain="fixture.test")
        elif change == "header":
            http.headers["authorization"] = "Bearer fixture-B"
        elif change == "auth":
            http.auth = httpx.BasicAuth("different", "authority")
        elif change == "endpoint":
            c.url = "http://fixture.test/changed"
        elif change == "session":
            c.session = object()
        elif change == "retired":
            c.retire()
        release.set()
        with pytest.raises(
            MCPProtocolError, match="HTTP approval refused before this send"
        ):
            await asyncio.wait_for(task, 2)
    c.session = old_session
    assert connected.effects == []
    assert not c._approval_http.pending


@pytest.mark.asyncio
async def test_cancelled_queued_call_cannot_send_late(connected, monkeypatch):
    c = connected.client
    entered, release, refused = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original = StreamableHTTPTransport._handle_post_request

    async def delayed(self, ctx):
        if rpc_method(ctx) == "tools/call":
            entered.set()
            await release.wait()
        return await original(self, ctx)

    monkeypatch.setattr(StreamableHTTPTransport, "_handle_post_request", delayed)
    original_send = c._approval_http.transport.send

    async def observed_send(request, **kwargs):
        response = await original_send(request, **kwargs)
        if response.extensions.get("mcp.local_approval_refusal"):
            refused.set()
        return response

    monkeypatch.setattr(c._approval_http.transport, "send", observed_send)
    with c.approval_scope(c.approval_binding(), "write", {"value": 1}):
        task = asyncio.create_task(c.call_tool("write", {"value": 1}))
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    release.set()
    await asyncio.wait_for(refused.wait(), 2)
    assert not c._approval_http.pending
    assert connected.effects == []


@pytest.mark.asyncio
async def test_fresh_cookie_scope_changes_key_without_reusing_old_rule(connected):
    c = connected.client
    old = c.approval_binding()
    connected.http.cookies.set("authority", "new", domain="fixture.test")
    new = c.approval_binding()
    assert new["key"] != old["key"]
    with pytest.raises(ValueError, match="changed"):
        with c.approval_scope(old, "write", {"value": 1}):
            pytest.fail("old authority was reused")
    with c.approval_scope(new, "write", {"value": 1}):
        await c.call_tool("write", {"value": 1})
    assert connected.effects[0]["headers"]["cookie"] == "authority=new"


@pytest.mark.asyncio
async def test_concurrent_actual_response_cookie_cannot_change_queued_authority(
    connected, monkeypatch
):
    c = connected.client
    entered, release = asyncio.Event(), asyncio.Event()
    original = StreamableHTTPTransport._handle_post_request

    async def delayed(self, ctx):
        if rpc_method(ctx) == "tools/call":
            entered.set()
            await release.wait()
        return await original(self, ctx)

    monkeypatch.setattr(StreamableHTTPTransport, "_handle_post_request", delayed)
    with c.approval_scope(c.approval_binding(), "write", {"value": 1}):
        task = asyncio.create_task(c.call_tool("write", {"value": 1}))
        await asyncio.wait_for(entered.wait(), 2)
        await c.read_resource("fixture://refresh")
        assert connected.http.cookies.get("authority") == "response-B"
        release.set()
        with pytest.raises(
            MCPProtocolError, match="HTTP approval refused before this send"
        ):
            await asyncio.wait_for(task, 2)
    assert connected.effects == []


@pytest.mark.asyncio
async def test_redirect_is_not_replay_authority_but_ordinary_once_still_works(
    connected,
):
    c = connected.client
    connected.state.redirect = True
    with c.approval_scope(c.approval_binding(), "write", {"value": 1}):
        with pytest.raises(
            MCPProtocolError, match="HTTP approval refused before this send"
        ):
            await c.call_tool("write", {"value": 1})
    assert connected.effects == []
    await c.call_tool("write", {"value": 2})
    assert len(connected.effects) == 1
    assert connected.effects[0]["params"]["arguments"] == {"value": 2}


@pytest.mark.asyncio
async def test_modern_parameter_header_is_bound_to_sdk_mapping(connected):
    c = connected.client
    args = {"value": 1, "tenant": "fixture"}
    with c.approval_scope(c.approval_binding(), "write", args):
        await c.call_tool("write", args)
    if connected.modern:
        assert connected.effects[0]["headers"]["mcp-param-tenant"] == "fixture"
    else:
        assert "mcp-param-tenant" not in connected.effects[0]["headers"]


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["authorization", "cookie", "url", "method"])
async def test_final_constructed_request_drift_is_refused(
    connected, monkeypatch, change
):
    c, http = connected.client, connected.http
    original_send = http.send

    async def changed_request(request, **kwargs):
        if json.loads(request.content).get("method") == "tools/call":
            if change in ("authorization", "cookie"):
                request.headers[change] = "changed-wire-authority"
            elif change == "url":
                request.url = httpx.URL("http://fixture.test/changed-target")
            else:
                request.method = "GET"
        return await original_send(request, **kwargs)

    monkeypatch.setattr(http, "send", changed_request)
    with c.approval_scope(c.approval_binding(), "write", {"value": 1}):
        with pytest.raises(
            MCPProtocolError, match="HTTP approval refused before this send"
        ):
            await c.call_tool("write", {"value": 1})
    assert connected.effects == []
    assert connected.state.unexpected_gets == []


@pytest.mark.asyncio
async def test_redirect_cannot_discard_approval_by_replacing_post_with_get(connected):
    c = connected.client
    connected.state.redirect = True
    connected.state.redirect_status = 303
    # SDK 2 refuses 303 itself; SDK 1 delegates redirects to HTTPX, where our
    # per-send guard must retain authority even after POST becomes GET.
    reason = (
        "HTTP approval refused before this send"
        if SDK_MAJOR == 1
        else "Redirect to .* not followed"
    )
    with c.approval_scope(c.approval_binding(), "write", {"value": 1}):
        with pytest.raises(MCPProtocolError, match=reason):
            await c.call_tool("write", {"value": 1})
    assert connected.effects == []
    assert connected.state.unexpected_gets == []


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [b"grant_type=client_credentials", b"\x00\xffopaque"])
async def test_ordinary_custom_auth_can_send_opaque_post(connected, body):
    class TokenAuth(httpx.Auth):
        async def async_auth_flow(self, request):
            token_request = httpx.Request(
                "POST",
                "http://fixture.test/oauth/token",
                content=body,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            response = yield token_request
            await response.aread()
            request.headers["Authorization"] = (
                "Bearer " + response.json()["access_token"]
            )
            yield request

    c = connected.client
    connected.http.auth = TokenAuth()
    with pytest.raises(ValueError, match="authorization pipeline is unsupported"):
        c.approval_binding()
    await asyncio.wait_for(c.call_tool("write", {"value": 1}), 3)
    assert connected.state.auth_posts == [body]
    assert len(connected.effects) == 1
    assert (
        connected.effects[0]["headers"]["authorization"] == "Bearer finite-auth-result"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [b"opaque", b"\x00\xffopaque"])
async def test_tagged_approved_request_cannot_become_opaque(
    connected, monkeypatch, body
):
    c, http = connected.client, connected.http
    original = http.send

    async def changed(request, **kwargs):
        request._content = body
        request.stream = httpx.ByteStream(body)
        request.headers["content-length"] = str(len(body))
        return await original(request, **kwargs)

    monkeypatch.setattr(http, "send", changed)
    with c.approval_scope(c.approval_binding(), "write", {"value": 1}):
        with pytest.raises(
            MCPProtocolError, match="HTTP approval refused before this send"
        ):
            await asyncio.wait_for(c.call_tool("write", {"value": 1}), 3)
    assert connected.effects == []
    assert connected.state.auth_posts == []
