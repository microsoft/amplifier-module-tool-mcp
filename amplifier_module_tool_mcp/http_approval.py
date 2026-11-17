"""Bind optional approval to a queued RPC and the HTTP request actually sent.

The SDK owns serialization, authentication and network transport. Its private
HTTP client runs our request hook after authentication and request construction,
immediately before handing that request to the transport. A captured queue entry
survives the SDK's task boundary, but never the original call's cancellation.
There is no durable policy or automatic retry here.
"""

from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import importlib
import json
from pydantic import RootModel

from .approval_binding import digest, request_digest


_CALL = ContextVar("mcp_http_approval_call", default=None)
_REQUEST_APPROVAL = "amplifier.mcp.approval_send"


class HTTPApprovalRefused(ValueError):
    """This individual HTTP send was refused locally before transport dispatch."""


def _wire(request):
    headers = {}
    for key, value in request.headers.raw:
        # Header names are case-insensitive; duplicate value order can affect
        # authorization and must not be sorted away.
        headers.setdefault(key.decode("latin1").lower(), []).append(
            value.decode("latin1")
        )
    return digest(
        {
            "method": request.method,
            "url": str(request.url),
            "headers": headers,
            "body": request.content.decode("utf-8"),
        }
    )


def _parameter_headers(session):
    maps = getattr(session, "_x_mcp_header_maps", {})
    if type(maps) is not dict:
        raise ValueError("Unsupported MCP parameter-header registry")
    result = {}
    for name, mapping in maps.items():
        if type(name) is not str or type(mapping) is not dict:
            raise ValueError("Unsupported MCP parameter-header mapping")
        rows = []
        for path, header in mapping.items():
            if (
                type(path) is not tuple
                or not all(type(part) is str for part in path)
                or type(header) is not str
            ):
                raise ValueError("Unsupported MCP parameter-header path")
            rows.append([list(path), header])
        result[name] = sorted(rows)
    return result


@dataclass(eq=False)
class _Call:
    guard: object
    session: object
    tool: str
    arguments: str
    approval: object
    active: bool = True
    queued: bool = False
    request_ids: set = field(default_factory=set)


class HTTPApprovalGuard:
    """Connection-local, bounded by active calls; holds no reusable rules."""

    def __init__(self, client, http):
        self.client = client
        self.http = http
        self.configuration = digest(client._approval_configuration())
        self.session = None
        self.pending = {}
        self.hook = self.before_send
        self.transport = AuthorizedHTTPClient(http, self)
        # This HTTP client is private to this connection. Keep existing SDK
        # defaults (timeouts, TLS, proxies and ordinary redirects) unchanged.
        http.event_hooks["request"].append(self.hook)

    def _session_id(self):
        get_id = getattr(self.client, "_get_session_id", None)
        return get_id() if get_id is not None else None

    def describe(self):
        client = self.client
        if (
            not client.is_connected
            or client._retired
            or self.session is None
            or client.session is not self.session
            or not client.protocol_version
            or digest(client._approval_configuration()) != self.configuration
        ):
            raise ValueError("The HTTP approval connection is no longer current")
        # Custom auth flows can send additional requests or consult mutable
        # external authority. They need their own trusted binding contract.
        if self.http.auth is not None or self.http.event_hooks["request"] != [
            self.hook
        ]:
            raise ValueError("The HTTP authorization pipeline is unsupported")
        request = self.http.build_request("POST", client.url, json={})
        if request.url.username or request.url.password:
            raise ValueError("URL credential authorization is unsupported")
        # SDK parameter-header mappings affect gateway authorization. Bind the
        # actual session cache as well as the wrapper's discovered tool schema.
        return {
            "version": 1,
            "httpSendAuthorization": 1,
            "key": digest(
                {
                    "configuration": self.configuration,
                    "protocol": client.protocol_version,
                    "request": _wire(request),
                    "parameterHeaders": _parameter_headers(self.session),
                }
            ),
        }

    @contextmanager
    def call(self, tool, arguments, approval):
        call = _Call(
            self, self.client.session, tool, request_digest(arguments), approval
        )
        token = _CALL.set(call)
        try:
            yield
        finally:
            call.active = False
            for request_id in call.request_ids:
                self.pending.pop(request_id, None)
            _CALL.reset(token)

    def _check(self, call):
        if (
            not call.active
            or call.guard is not self
            or self.client._retired
            or self.client.session is not call.session
            or not self.client.is_connected
        ):
            raise HTTPApprovalRefused("The HTTP tool call is no longer active")
        approval = call.approval
        if approval is not None:
            try:
                current = self.describe()
            except ValueError as error:
                raise HTTPApprovalRefused(
                    "The HTTP authorization pipeline changed"
                ) from error
            if (
                not approval.active
                or not approval.consumed
                or approval.client is not self.client
                or approval.session is not call.session
                or approval.binding != current
            ):
                raise HTTPApprovalRefused(
                    "The approved HTTP authorization changed before send"
                )

    def queued(self, message):
        # SDK 1.x serializes its JSONRPCMessage RootModel with exclude_none;
        # 2.x serializes concrete RPC models with exclude_unset. Bind the
        # actual supported serializer, including explicit null arguments.
        flags = (
            {"exclude_none": True}
            if isinstance(message.message, RootModel)
            else {"exclude_unset": True}
        )
        payload = message.message.model_dump(by_alias=True, mode="json", **flags)
        if payload.get("method") != "tools/call":
            return
        call = _CALL.get()
        if call is None or call.guard is not self:
            raise ValueError("HTTP tool request has no current invocation")
        self._check(call)
        params = payload.get("params", {})
        request_id = payload.get("id")
        if (
            type(request_id) not in (int, str)
            or request_id in self.pending
            or call.queued
            or params.get("name") != call.tool
            or request_digest(params.get("arguments")) != call.arguments
        ):
            raise ValueError("The queued HTTP tool request changed")
        expected = None
        if call.approval is not None:
            # No continuation/resumption or new authority can be smuggled into
            # the exact fresh call this API exposes.
            if (
                any(
                    k not in ("name", "arguments", "_meta") and v is not None
                    for k, v in params.items()
                )
                or type(params.get("_meta", {})) is not dict
            ):
                raise ValueError("HTTP tool continuation metadata is unsupported")
            metadata = getattr(message, "metadata", None)
            extra = getattr(metadata, "headers", None) or {}
            if (
                getattr(metadata, "resumption_token", None) is not None
                or type(extra) is not dict
                or any(
                    type(k) is not str
                    or type(v) is not str
                    or not (
                        k.lower() in ("mcp-protocol-version", "mcp-name", "mcp-method")
                        or k.lower().startswith("mcp-param-")
                    )
                    for k, v in extra.items()
                )
            ):
                raise ValueError("HTTP tool request headers are unsupported")
            headers = {
                "accept": "application/json, text/event-stream",
                "content-type": "application/json",
                "mcp-protocol-version": self.client.protocol_version,
            }
            session_id = self._session_id()
            if session_id:
                headers["mcp-session-id"] = session_id
            headers.update({k.lower(): v for k, v in extra.items()})
            if headers["mcp-protocol-version"] != self.client.protocol_version:
                raise ValueError("The HTTP protocol changed before send")
            # This is an immutable digest of the complete expected wire request,
            # including effective cookies/headers. Never log credential values.
            expected = _wire(
                self.http.build_request(
                    "POST", self.client.url, json=payload, headers=headers
                )
            )
        call.queued = True
        call.request_ids.add(request_id)
        self.pending[request_id] = [call, expected, False]

    def _entry(self, request):
        tagged = request.extensions.get(_REQUEST_APPROVAL)
        if tagged is not None:
            # HTTPX preserves extensions when constructing redirect requests,
            # including redirects that replace POST/body with GET/no body.
            if (
                type(tagged) is not list
                or len(tagged) != 3
                or not isinstance(tagged[0], _Call)
                or not any(self.pending.get(i) is tagged for i in tagged[0].request_ids)
            ):
                raise HTTPApprovalRefused(
                    "The HTTP request authority is no longer current"
                )
            self._check(tagged[0])
            return tagged
        if request.method != "POST":
            return
        payload = json.loads(request.content)
        if not isinstance(payload, dict):
            return
        entry = self.pending.get(payload.get("id"))
        if entry is None:
            if payload.get("method") != "tools/call":
                return
            raise HTTPApprovalRefused(
                "HTTP tool request was withdrawn or never registered"
            )
        call, _, _ = entry
        self._check(call)
        return entry

    def before_auth(self, request):
        # Refuse a substituted auth flow before it can itself do I/O. The final
        # request hook still checks the post-auth wire request, including cookies.
        entry = self._entry(request)
        if entry is not None:
            request.extensions[_REQUEST_APPROVAL] = entry
        return entry

    async def before_send(self, request):
        # HTTPX calls request hooks after auth, on each actual send. There is no
        # await between this check and returning to its transport dispatch.
        entry = self._entry(request)
        if entry is None:
            return
        call, expected, sent = entry
        if expected is not None:
            if sent:
                # A redirect may carry a streaming or empty replacement body.
                # No second send is authorized, so refuse before reading it.
                raise HTTPApprovalRefused("The approved HTTP request was already sent")
            try:
                wire = _wire(request)
            except (ValueError, UnicodeError) as error:
                raise HTTPApprovalRefused(
                    "The approved HTTP request is no longer exact"
                ) from error
            if wire != expected:
                raise HTTPApprovalRefused(
                    "The approved HTTP request changed before send"
                )
            # A redirect/auth challenge must not silently resend an approved
            # tool operation. Ordinary unscoped SDK behavior is unchanged.
            entry[2] = True


class AuthorizedHTTPClient:
    """Use the public HTTP client API; keep its TLS/proxy/auth implementation."""

    def __init__(self, client, guard):
        self.client, self.guard = client, guard

    def __getattr__(self, name):
        return getattr(self.client, name)

    async def send(self, request, **kwargs):
        # SDK redirects may wrap the already-buffered JSON body in a stream.
        # Materialize that body before the checks; no authority is consumed yet.
        if request.method == "POST":
            await request.aread()
        try:
            entry = self.guard.before_auth(request)
            if (
                entry is not None
                and entry[1] is not None
                and kwargs.get("auth") is not None
            ):
                raise HTTPApprovalRefused(
                    "Per-request HTTP authentication is unsupported"
                )
            return await self.client.send(request, **kwargs)
        except HTTPApprovalRefused:
            # A local refusal belongs to this RPC, not every waiter sharing the
            # connection. It neither sends nor retries the refused request. A
            # prior sent request (e.g. a redirect response) remains historical.
            payload = json.loads(request.content)
            module = type(self.client).__module__.split(".")[0]
            if module not in ("httpx", "httpx2"):
                raise
            response = importlib.import_module(module).Response
            return response(
                200,
                request=request,
                json={
                    "jsonrpc": "2.0",
                    "id": payload["id"],
                    "error": {
                        "code": -32000,
                        "message": "HTTP approval refused before this send.",
                    },
                },
                extensions={"mcp.local_approval_refusal": True},
            )

    @asynccontextmanager
    async def stream(self, method, url, **kwargs):
        # Older supported SDKs use stream(); current SDKs call build_request +
        # send directly. Both enter the same pre-auth and final-send checks.
        send = {k: kwargs.pop(k) for k in ("auth", "follow_redirects") if k in kwargs}
        request = self.client.build_request(method, url, **kwargs)
        response = await self.send(request, stream=True, **send)
        try:
            yield response
        finally:
            await response.aclose()


class ApprovalSendStream:
    """Observe the SDK's exact RPC ID before it crosses the transport queue."""

    def __init__(self, stream, guard):
        self.stream, self.guard = stream, guard

    async def send(self, message):
        self.guard.queued(message)
        await self.stream.send(message)

    def send_nowait(self, message):
        self.guard.queued(message)
        self.stream.send_nowait(message)

    async def aclose(self):
        await self.stream.aclose()

    def close(self):
        self.stream.close()

    def clone(self):
        return type(self)(self.stream.clone(), self.guard)

    async def __aenter__(self):
        await self.stream.__aenter__()
        return self

    async def __aexit__(self, *args):
        return await self.stream.__aexit__(*args)
