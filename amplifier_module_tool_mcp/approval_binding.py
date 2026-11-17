"""Ephemeral connection facts for an optional caller-owned approval policy.

No rules or decisions live here. A bound invocation may use only the connected
session it inspected, including after an await or attempted reconnect.
"""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import hashlib
import json


@dataclass
class _Invocation:
    client: object
    session: object
    binding: dict
    tool: str
    arguments: str
    active: bool = True
    consumed: bool = False


_EXPECTED: ContextVar[_Invocation | None] = ContextVar("mcp_approval_connection", default=None)


def digest(value):
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode()).hexdigest()


def request_digest(arguments):
    def exact(value):
        if value is None or type(value) in (str, bool, int, float):
            return
        if type(value) is list:
            for item in value:
                exact(item)
            return
        if type(value) is dict and all(type(key) is str for key in value):
            for item in value.values():
                exact(item)
            return
        raise ValueError("The MCP invocation arguments are not exact JSON")
    exact(arguments)
    return digest(arguments)


def http_state(client):
    # These are the live public httpx request settings, not the original
    # constructor dictionary. Unsupported custom auth remains Once-only.
    if client.auth is not None:
        raise ValueError("Custom MCP HTTP authentication has no approval binding")
    return {
        "headers": list(client.headers.multi_items()),
        "cookies": [(cookie.domain, cookie.path, cookie.name, cookie.value,
                     cookie.secure, cookie.expires) for cookie in client.cookies.jar],
        "trustEnvironment": client.trust_env,
        "followRedirects": client.follow_redirects,
        "timeout": client.timeout.as_dict(),
    }


class ConnectionBinding:
    def __init__(self, client, session, configuration, facts, current_transport=None):
        self.client = client
        self.session = session
        self.configuration = digest(configuration)
        # Retain no credential/environment values in this object or diagnostics.
        self.fingerprint = digest(facts)
        self.protocol = facts.get("protocol")
        self.current_transport = current_transport

    def describe(self, configuration):
        if (self.client.session is not self.session or not self.client.is_connected
                or self.client._retired or digest(configuration) != self.configuration
                or not self.protocol or self.client._protocol_version != self.protocol):
            raise ValueError("The MCP approval connection is no longer current")
        return {"version": 1, "key": digest({
            "connection": self.fingerprint,
            "current": self.current_transport() if self.current_transport else None,
        })}


@contextmanager
def bind(client, expected, tool_name, arguments):
    current = client.approval_binding()
    if current != expected:
        raise ValueError("The MCP approval connection changed")
    if not isinstance(tool_name, str) or not tool_name or type(arguments) is not dict:
        raise ValueError("The MCP invocation is not an exact tool request")
    invocation = _Invocation(client, client.session, current, tool_name, request_digest(arguments))
    token = _EXPECTED.set(invocation)
    try:
        yield
    finally:
        invocation.active = False
        _EXPECTED.reset(token)


def check(client, tool_name, arguments, *, consume=False):
    expected = _EXPECTED.get()
    if expected is None:
        return
    if (not expected.active or expected.consumed or expected.client is not client
            or client.session is not expected.session or expected.tool != tool_name
            or request_digest(arguments) != expected.arguments
            or client.approval_binding() != expected.binding):
        raise ValueError("The approved MCP connection changed before invocation")
    if consume:
        expected.consumed = True
