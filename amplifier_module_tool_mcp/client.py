"""MCP Client wrapper for connecting to and communicating with MCP servers."""

import asyncio
import logging
from enum import Enum
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from amplifier_module_tool_mcp.discovery import (
    discover_prompts,
    discover_resources,
    discover_tools,
)
from amplifier_module_tool_mcp.reconnection import (
    CircuitBreaker,
    ReconnectionConfig,
    ReconnectionStrategy,
)
from amplifier_module_tool_mcp.sdk_compat import (
    MCP_ERROR_CLASS,
    MCPProtocolError,
    MRTRNotSupportedError,
    build_client_info,
    describe_mcp_error,
    extract_root_cause,
    is_modern_protocol_version,
    is_mrtr_unsupported_error,
    negotiate,
    supports_log_level_kwarg,
)

logger = logging.getLogger(__name__)


class ConnectionState(Enum):
    """Connection state enumeration."""

    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    DISCONNECTING = "disconnecting"
    ERROR = "error"


class MCPClient:
    """
    MCP client that manages connection to a single MCP server.

    Handles:
    - Server process lifecycle (start/stop)
    - Tool discovery via list_tools
    - Tool execution via call_tool
    - Reconnection with exponential backoff
    - Circuit breaker for failing servers
    - Health monitoring

    The client maintains an active connection by managing the async context
    managers for the stdio transport and session.
    """

    def __init__(
        self,
        server_name: str,
        command: str,
        args: list[str],
        env: dict[str, str] | None = None,
        reconnection_config: ReconnectionConfig | None = None,
        verbose_servers: bool = False,
        server_log_dir: Path | None = None,
    ):
        """
        Initialize MCP client.

        Args:
            server_name: Unique name for this server
            command: Command to execute (e.g., "npx", "python")
            args: Arguments for the command
            env: Optional environment variables
            reconnection_config: Reconnection configuration (uses defaults if None)
            verbose_servers: Whether to show server stderr output (default: False)
            server_log_dir: Directory for server logs when suppressed (default: ~/.amplifier/logs/mcp-servers/)
        """
        self.server_name = server_name
        self.command = command
        self.args = args
        self.env = env or {}
        self.session: ClientSession | None = None
        self.tools: list[dict[str, Any]] = []
        self.resources: list[dict[str, Any]] = []
        self.prompts: list[dict[str, Any]] = []
        self._state = ConnectionState.DISCONNECTED

        # Negotiated protocol version (e.g. "2026-07-28", or a legacy
        # handshake version like "2025-03-26"). None until connect() has
        # run at least once. See `protocol_version` property.
        self._protocol_version: str | None = None

        # Desired log level, cached for the *next* ClientSession
        # construction. `logging/setLevel` was removed in 2026-07-28; on a
        # modern-negotiated session, log level can only be set at
        # ClientSession construction time (see `set_logging_level`).
        self._log_level: str | None = None

        # Background task for connection lifecycle
        self._connection_task: asyncio.Task | None = None
        self._ready_event = asyncio.Event()
        self._shutdown_event = asyncio.Event()
        self._connection_error: BaseException | None = None

        # Reconnection and health management
        self._reconnection_strategy = ReconnectionStrategy(reconnection_config)
        self._circuit_breaker = CircuitBreaker()
        self._connection_attempts = 0
        self._last_error: Exception | None = None

        # Server output control
        self.verbose_servers = verbose_servers
        self.server_log_dir = (
            server_log_dir or Path("~/.amplifier/logs/mcp-servers/").expanduser()
        )
        self._server_log_file = None

    @property
    def state(self) -> ConnectionState:
        """Get current connection state."""
        return self._state

    @property
    def is_connected(self) -> bool:
        """Check if client is connected."""
        return self._state == ConnectionState.CONNECTED

    @property
    def protocol_version(self) -> str | None:
        """Negotiated MCP protocol version (e.g. "2026-07-28"), or None before connect()."""
        return self._protocol_version

    @property
    def health_status(self) -> dict[str, Any]:
        """
        Get health status of the connection.

        Returns:
            Dictionary with health information
        """
        return {
            "server_name": self.server_name,
            "state": self._state.value,
            "is_connected": self.is_connected,
            "circuit_breaker_state": self._circuit_breaker.state,
            "connection_attempts": self._connection_attempts,
            "tools_discovered": len(self.tools),
            "resources_discovered": len(self.resources),
            "prompts_discovered": len(self.prompts),
            "last_error": str(self._last_error) if self._last_error else None,
        }

    async def _connection_task_impl(self) -> None:
        """Background task that owns the connection lifecycle.

        This task enters all context managers, stays alive until shutdown signal,
        then exits all contexts properly in the same task context.
        """
        try:
            # Create server parameters with inherited environment
            import os

            if self.env:
                # Inherit all parent env vars, then override with server config
                merged_env = {**os.environ, **self.env}
            else:
                # Just use parent environment
                merged_env = os.environ.copy()

            server_params = StdioServerParameters(
                command=self.command,
                args=self.args,
                env=merged_env,
            )

            # Open log file if needed
            log_file = None
            if not self.verbose_servers:
                self.server_log_dir.mkdir(parents=True, exist_ok=True)
                log_file_path = self.server_log_dir / f"{self.server_name}.log"
                log_file = open(log_file_path, "a", encoding="utf-8")
                logger.debug(
                    f"Server '{self.server_name}' output redirected to: {log_file_path}"
                )
            else:
                logger.debug(
                    f"Server '{self.server_name}' output will appear in console"
                )

            try:
                # Enter stdio_client context - stays in THIS task
                async with stdio_client(server_params, errlog=log_file) as (
                    read,
                    write,
                ):
                    # Build ClientSession kwargs. `client_info` identifies
                    # this module (rather than the SDK's own "mcp/0.1.0"
                    # placeholder) to the server on both SDK majors.
                    # `log_level` is only accepted by mcp>=2.0 -- pass it
                    # only when supported and a level has been requested via
                    # a prior set_logging_level() call.
                    session_kwargs: dict[str, Any] = {
                        "client_info": build_client_info()
                    }
                    if self._log_level is not None and supports_log_level_kwarg():
                        session_kwargs["log_level"] = self._log_level

                    # Enter ClientSession context - stays in THIS task
                    async with ClientSession(read, write, **session_kwargs) as session:
                        # Negotiate protocol version. On mcp>=2.0 this drives
                        # the full modern stateless handshake (server/discover
                        # + automatic legacy fallback); on mcp<2.0 (no
                        # negotiate_auto) it falls back to the legacy
                        # initialize() and logs that degradation once.
                        self._protocol_version = await negotiate(
                            session, server_name=self.server_name
                        )

                        # Store session and signal ready
                        self.session = session
                        await self._discover_capabilities()
                        self._state = ConnectionState.CONNECTED
                        self._circuit_breaker.record_success()
                        self._last_error = None
                        self._ready_event.set()

                        logger.info(
                            f"Connected to MCP server '{self.server_name}' "
                            f"(protocol {self._protocol_version or 'unknown'}) - "
                            f"discovered {len(self.tools)} tools, {len(self.resources)} resources, "
                            f"{len(self.prompts)} prompts"
                        )

                        # Stay alive until shutdown signal
                        await self._shutdown_event.wait()

                        logger.debug(
                            f"Shutting down connection to '{self.server_name}'"
                        )
                        # Exiting contexts here cleans up properly in THIS task
            finally:
                if log_file:
                    log_file.close()

        except BaseException as e:
            # Use ``except BaseException`` (not ``except Exception``) so that
            # ``asyncio.CancelledError`` -- which inherits from
            # ``BaseException``, not ``Exception``, since Python 3.8 -- is
            # always caught.  Without this, a cancellation during
            # ``session.initialize()`` or ``_discover_capabilities()`` would
            # bypass the ``_ready_event.set()`` call and leave ``connect()``
            # blocked forever (mirrors the fix in streamable_http_client.py).
            #
            # Unwrap anyio ExceptionGroup -> real transport error, matching
            # the streamable_http_client.py transport (both paths must be
            # consistent about surfacing the real root cause).
            root_cause = extract_root_cause(e)

            self._connection_error = root_cause
            self._state = ConnectionState.ERROR

            # Only record as _last_error / circuit-breaker failure for
            # genuine transport errors, not for external task cancellation.
            if not isinstance(e, asyncio.CancelledError):
                self._last_error = (
                    root_cause if isinstance(root_cause, Exception) else None
                )
                self._circuit_breaker.record_failure()

            # Always unblock connect() so it never hangs.
            self._ready_event.set()

            if isinstance(e, asyncio.CancelledError):
                logger.debug(f"Connection task for '{self.server_name}' was cancelled")
            else:
                # Include log file location in error message if suppressed
                error_msg = f"Failed to connect to MCP server '{self.server_name}': {root_cause}"
                if not self.verbose_servers:
                    log_file_path = self.server_log_dir / f"{self.server_name}.log"
                    error_msg += f"\nServer logs available at: {log_file_path}"
                logger.error(error_msg)

        finally:
            # Clear state
            self.session = None
            self._state = ConnectionState.DISCONNECTED
            logger.debug(f"Connection task for '{self.server_name}' completed")

    async def connect(self) -> None:
        """Connect to MCP server (starts background task)."""
        if self._connection_task is not None:
            return  # Already connected

        # Check circuit breaker
        if self._circuit_breaker.is_open():
            logger.warning(
                f"Circuit breaker is OPEN for '{self.server_name}' - blocking connection attempt"
            )
            raise RuntimeError(
                f"Circuit breaker is OPEN for server '{self.server_name}' - too many recent failures"
            )

        self._state = ConnectionState.CONNECTING
        self._connection_attempts += 1

        # Reset coordination
        self._ready_event.clear()
        self._shutdown_event.clear()
        self._connection_error = None

        # Start background task
        self._connection_task = asyncio.create_task(
            self._connection_task_impl(), name=f"mcp-{self.server_name}"
        )

        # Wait for ready signal
        await self._ready_event.wait()

        # Check for startup errors
        if self._connection_error:
            await self._connection_task
            self._connection_task = None
            # self._connection_error is already the unwrapped root cause (see
            # _connection_task_impl), so this message is self-describing
            # instead of surfacing the opaque anyio "unhandled errors in a
            # TaskGroup (1 sub-exception)" wrapper.
            root_cause = self._connection_error
            raise RuntimeError(
                f"MCP server connection failed: {type(root_cause).__name__}: {root_cause}"
            ) from root_cause

    async def connect_with_retry(self) -> None:
        """Connect with automatic retry on failure."""

        async def _connect() -> None:
            await self.connect()

        await self._reconnection_strategy.execute_with_retry(
            _connect, f"connect to {self.server_name}"
        )

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        """
        Call a tool on the MCP server.

        Args:
            tool_name: Name of the tool to call
            arguments: Arguments to pass to the tool

        Returns:
            Tool execution result
        """
        if not self.is_connected or not self.session:
            await self.connect()

        try:
            result = await self.session.call_tool(tool_name, arguments=arguments)
            self._circuit_breaker.record_success()
            return result

        except MCP_ERROR_CLASS as e:
            # A well-formed JSON-RPC protocol error -- the server understood
            # the request and rejected it with a real error code. Preserve
            # code/message/data/explanation via MCPProtocolError so callers
            # (wrapper.py) can distinguish this from a transport failure.
            self._circuit_breaker.record_failure()
            self._last_error = e
            info = describe_mcp_error(e)
            logger.error(
                f"Tool call failed for '{tool_name}' on '{self.server_name}': "
                f"MCP error {info['code']} -- {info['explanation']}"
            )
            raise MCPProtocolError(
                f"Tool execution failed: {info['message']}",
                code=info["code"],
                message=info["message"],
                data=info["data"],
                explanation=info["explanation"],
            ) from e

        except Exception as e:
            self._circuit_breaker.record_failure()
            self._last_error = e

            if is_mrtr_unsupported_error(e):
                # The SDK's own message here ("pass allow_input_required=True
                # ... retry call_tool(..., input_responses=...)") is not
                # actionable by this caller -- this module exposes no such
                # parameters. Replace it with an honest statement of what
                # actually happened rather than an instruction the caller
                # cannot follow.
                message = (
                    f"MCP server '{self.server_name}' requires interactive "
                    f"input (MRTR / InputRequiredResult) to complete tool "
                    f"'{tool_name}'. This module does not yet support the "
                    f"multi-round-trip input protocol -- the call cannot be "
                    f"completed."
                )
                logger.error(message)
                raise MRTRNotSupportedError(message) from e

            logger.error(
                f"Tool call failed for '{tool_name}' on '{self.server_name}': {e}"
            )
            raise RuntimeError(f"Tool execution failed: {e}") from e

    async def call_tool_with_retry(
        self, tool_name: str, arguments: dict[str, Any]
    ) -> Any:
        """
        Call a tool with automatic retry on failure.

        Args:
            tool_name: Name of the tool to call
            arguments: Arguments to pass to the tool

        Returns:
            Tool execution result
        """

        async def _call() -> Any:
            return await self.call_tool(tool_name, arguments)

        return await self._reconnection_strategy.execute_with_retry(
            _call, f"call tool {tool_name} on {self.server_name}"
        )

    async def _discover_capabilities(self) -> None:
        """Discover tools, resources, and prompts from server.

        Delegates to `amplifier_module_tool_mcp.discovery`, which follows
        pagination cursors to completion (a server returning `nextCursor`
        is no longer silently truncated) and is shared verbatim with
        `MCPStreamableHTTPClient` so both transports stay in lockstep.
        """
        if not self.session:
            return

        self.tools = await discover_tools(self.session)
        self.resources = await discover_resources(
            self.session, server_name=self.server_name
        )
        self.prompts = await discover_prompts(
            self.session, server_name=self.server_name
        )

    async def read_resource(self, uri: str) -> Any:
        """Read a resource from the MCP server."""
        if not self.is_connected or not self.session:
            await self.connect()

        try:
            result = await self.session.read_resource(uri=uri)
            self._circuit_breaker.record_success()
            return result

        except MCP_ERROR_CLASS as e:
            self._circuit_breaker.record_failure()
            self._last_error = e
            info = describe_mcp_error(e)
            logger.error(
                f"Resource read failed for '{uri}' on '{self.server_name}': "
                f"MCP error {info['code']} -- {info['explanation']}"
            )
            raise MCPProtocolError(
                f"Resource read failed: {info['message']}",
                code=info["code"],
                message=info["message"],
                data=info["data"],
                explanation=info["explanation"],
            ) from e

        except Exception as e:
            self._circuit_breaker.record_failure()
            self._last_error = e
            logger.error(
                f"Resource read failed for '{uri}' on '{self.server_name}': {e}"
            )
            raise RuntimeError(f"Resource read failed: {e}") from e

    async def get_prompt(
        self, name: str, arguments: dict[str, Any] | None = None
    ) -> Any:
        """Get a prompt from the MCP server."""
        if not self.is_connected or not self.session:
            await self.connect()

        try:
            result = await self.session.get_prompt(name=name, arguments=arguments or {})
            self._circuit_breaker.record_success()
            return result

        except MCP_ERROR_CLASS as e:
            self._circuit_breaker.record_failure()
            self._last_error = e
            info = describe_mcp_error(e)
            logger.error(
                f"Get prompt failed for '{name}' on '{self.server_name}': "
                f"MCP error {info['code']} -- {info['explanation']}"
            )
            raise MCPProtocolError(
                f"Get prompt failed: {info['message']}",
                code=info["code"],
                message=info["message"],
                data=info["data"],
                explanation=info["explanation"],
            ) from e

        except Exception as e:
            self._circuit_breaker.record_failure()
            self._last_error = e
            logger.error(f"Get prompt failed for '{name}' on '{self.server_name}': {e}")
            raise RuntimeError(f"Get prompt failed: {e}") from e

    async def set_logging_level(self, level: str) -> None:
        """Set the logging level for the MCP server.

        `logging/setLevel` was removed from the protocol in 2026-07-28; log
        level is now negotiated once per session (via `_meta` on every
        request, driven by `ClientSession(log_level=...)`), not set via a
        standalone RPC.

        On a legacy-negotiated session, the old RPC is still correct and is
        sent as before. On a modern-negotiated session, the requested level
        is cached for the *next* connection (passed to `ClientSession` at
        construction) and this raises rather than silently no-op-ing or
        sending a method the server has every right to reject.
        """
        if is_modern_protocol_version(self._protocol_version):
            self._log_level = level
            logger.info(
                f"Cached log level '{level}' for server '{self.server_name}' -- "
                f"will apply on the next connection via ClientSession(log_level=...)."
            )
            raise RuntimeError(
                f"Cannot change logging level on an active modern-protocol "
                f"({self._protocol_version}) session for '{self.server_name}': "
                f"'logging/setLevel' was removed in 2026-07-28. The requested "
                f"level {level!r} has been cached and will take effect on the "
                f"next reconnect. To apply it now, call disconnect() then "
                f"connect() again."
            )

        if not self.is_connected or not self.session:
            await self.connect()

        try:
            await self.session.set_logging_level(level=level)
            logger.info(
                f"Set logging level to '{level}' for server '{self.server_name}'"
            )

        except Exception as e:
            logger.error(f"Failed to set logging level: {e}")
            raise

    async def disconnect(self) -> None:
        """Disconnect from MCP server (stops background task cleanly)."""
        if self._connection_task is None:
            return

        # Signal shutdown
        self._shutdown_event.set()

        # Wait for clean exit
        try:
            await self._connection_task
        except Exception as e:
            logger.warning(f"Error during {self.server_name} shutdown: {e}")
        finally:
            self._connection_task = None
            logger.info(f"Disconnected from MCP server '{self.server_name}'")

    async def reset_circuit_breaker(self) -> None:
        """Reset the circuit breaker (useful for manual recovery)."""
        self._circuit_breaker.reset()
        logger.info(f"Circuit breaker reset for '{self.server_name}'")

    def get_tools(self) -> list[dict[str, Any]]:
        """Get the list of discovered tools."""
        return self.tools

    def get_resources(self) -> list[dict[str, Any]]:
        """Get the list of discovered resources."""
        return self.resources

    def get_prompts(self) -> list[dict[str, Any]]:
        """Get the list of discovered prompts."""
        return self.prompts
