"""Optional public-client settings shared by the two transports."""

import math
from datetime import timedelta
from importlib.metadata import version


def validate_options(discovery, read_timeout_seconds):
    if type(discovery) is not str or discovery not in ("all", "tools"):
        raise ValueError("MCP discovery must be 'all' or 'tools'")
    if read_timeout_seconds is not None:
        if (
            type(read_timeout_seconds) not in (int, float)
            or not math.isfinite(read_timeout_seconds)
            or read_timeout_seconds <= 0
        ):
            raise ValueError("MCP read timeout must be a positive finite number")


def configuration_options(discovery, read_timeout_seconds):
    """Omit defaults so existing approval fingerprints remain unchanged."""
    options = {}
    if discovery != "all":
        options["discovery"] = discovery
    if read_timeout_seconds is not None:
        options["read_timeout_seconds"] = read_timeout_seconds
    return options


def session_timeout_kwargs(read_timeout_seconds):
    if read_timeout_seconds is None:
        return {}
    # Public ClientSession parameter: SDK 1.x takes timedelta, SDK 2.x seconds.
    major = int(version("mcp").split(".", 1)[0])
    if major == 1:
        timeout = timedelta(seconds=read_timeout_seconds)
    elif major == 2:
        timeout = float(read_timeout_seconds)
    else:
        raise ValueError("Unsupported MCP SDK read timeout API")
    return {"read_timeout_seconds": timeout}


def require_same_session(client, session):
    if session is None or client.session is not session or not client.is_connected:
        raise ValueError("The MCP connection changed before invocation")
