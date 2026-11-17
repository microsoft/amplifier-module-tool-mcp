"""Ephemeral connection facts for an optional caller-owned approval policy.

No rules or decisions live here. A bound invocation may use only the connected
session it inspected, including after an await or attempted reconnect.
"""

import asyncio
import hashlib
import inspect
import json
from collections.abc import Callable
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass


@dataclass
class _Invocation:
    client: object
    session: object
    binding: dict
    tool: str
    arguments: str
    is_current: Callable[[], bool] | None = None
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


class ConnectionBinding:
    def __init__(self, client, session, configuration, facts):
        self.client = client
        self.session = session
        self.configuration = digest(configuration)
        # Retain no credential/environment values in this object or diagnostics.
        self.fingerprint = digest(facts)
        self.protocol = facts.get("protocol")

    def describe(self, configuration):
        if (self.client.session is not self.session or not self.client.is_connected
                or self.client._retired or digest(configuration) != self.configuration
                or not self.protocol or self.client._protocol_version != self.protocol):
            raise ValueError("The MCP approval connection is no longer current")
        return {"version": 1, "key": self.fingerprint}


def require_current(invocation):
    """An optional owner veto, never an authority grant or cached decision."""
    if invocation.is_current is None:
        return
    try:
        current = invocation.is_current()
    except (Exception, asyncio.CancelledError):
        # A synchronous owner's cancellation veto must not cancel the shared
        # SDK HTTP worker and unrelated calls that happen to use it.
        invocation.active = False
        raise ValueError("The MCP invocation owner is no longer current") from None
    if current is not True:
        invocation.active = False
        if inspect.iscoroutine(current):
            current.close()  # An async callback is invalid; never schedule it.
        raise ValueError("The MCP invocation owner is no longer current")


@contextmanager
def bind(client, expected, tool_name, arguments, *, is_current=None):
    if is_current is not None and not callable(is_current):
        raise ValueError("The MCP invocation owner check must be callable")
    invocation = _Invocation(client, client.session, {}, tool_name, "", is_current)
    require_current(invocation)
    current = client.approval_binding()
    if current != expected:
        raise ValueError("The MCP approval connection changed")
    if not isinstance(tool_name, str) or not tool_name or type(arguments) is not dict:
        raise ValueError("The MCP invocation is not an exact tool request")
    invocation.session = client.session
    invocation.binding = current
    invocation.arguments = request_digest(arguments)
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
    require_current(expected)
    if (not expected.active or expected.consumed or expected.client is not client
            or client.session is not expected.session or expected.tool != tool_name
            or request_digest(arguments) != expected.arguments
            or client.approval_binding() != expected.binding):
        raise ValueError("The approved MCP connection changed before invocation")
    if consume:
        expected.consumed = True
    return expected
