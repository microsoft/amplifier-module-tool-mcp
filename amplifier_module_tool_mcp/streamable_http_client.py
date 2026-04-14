"""Streamable HTTP transport MCP client (2025-03-26 spec)."""

import asyncio
import logging
from typing import Any

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from amplifier_module_tool_mcp.reconnection import CircuitBreaker
from amplifier_module_tool_mcp.reconnection import ReconnectionConfig
from amplifier_module_tool_mcp.reconnection import ReconnectionStrategy

logger = logging.getLogger(__name__)


class MCPStreamableHTTPClient:
    """
    MCP client using Streamable HTTP transport (2025-03-26 spec).

    This is the newer HTTP transport that replaces HTTP+SSE in the
    latest MCP specification. It uses a single endpoint for both
    POST and GET requests with optional SSE streaming.
    """

    def __init__(
        self,
        server_name: str,
        url: str,
        headers: dict[str, str] | None = None,
        reconnection_config: ReconnectionConfig | None = None,
    ):
        """
        Initialize Streamable HTTP MCP client.

        Args:
            server_name: Unique name for this server
            url: HTTP endpoint URL
            headers: Optional HTTP headers
            reconnection_config: Reconnection configuration
        """
        self.server_name = server_name
        self.url = url
        self.headers = headers or {}
        self.session: ClientSession | None = None
        self.tools: list[dict[str, Any]] = []
        self.resources: list[dict[str, Any]] = []
        self.prompts: list[dict[str, Any]] = []
        self._connected = False

        # Background task for connection lifecycle
        self._connection_task: asyncio.Task | None = None
        self._ready_event = asyncio.Event()
        self._shutdown_event = asyncio.Event()
        self._connection_error: Exception | None = None

        # Reconnection and health management
        self._reconnection_strategy = ReconnectionStrategy(reconnection_config)
        self._circuit_breaker = CircuitBreaker()
        self._connection_attempts = 0
        self._last_error: Exception | None = None

    @property
    def is_connected(self) -> bool:
        """Check if client is connected."""
        return self._connected

    @property
    def health_status(self) -> dict[str, Any]:
        """Get health status of the connection."""
        return {
            "server_name": self.server_name,
            "transport": "streamable-http",
            "url": self.url,
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
            # Enter streamablehttp_client context - stays in THIS task
            async with streamablehttp_client(self.url, headers=self.headers) as (read, write, get_session_id):
                # Store session ID getter for resumability
                self._get_session_id = get_session_id

                # Enter ClientSession context - stays in THIS task
                async with ClientSession(read, write) as session:
                    # Initialize
                    await session.initialize()

                    # Store session and signal ready
                    self.session = session
                    await self._discover_capabilities()
                    self._connected = True
                    self._circuit_breaker.record_success()
                    self._last_error = None
                    self._ready_event.set()

                    logger.info(
                        f"Connected to Streamable HTTP MCP server '{self.server_name}' at {self.url} - "
                        f"discovered {len(self.tools)} tools, {len(self.resources)} resources, "
                        f"{len(self.prompts)} prompts"
                    )

                    # Stay alive until shutdown signal
                    await self._shutdown_event.wait()

                    logger.debug(f"Shutting down connection to '{self.server_name}'")
                    # Exiting contexts here cleans up properly in THIS task

        except Exception as e:
            # Store error and signal ready so connect() can raise it
            self._connection_error = e
            self._connected = False
            self._last_error = e
            self._circuit_breaker.record_failure()
            self._ready_event.set()

            logger.error(f"Failed to connect to Streamable HTTP MCP server '{self.server_name}': {e}")

        finally:
            # Clear state
            self.session = None
            self._connected = False
            logger.debug(f"Connection task for '{self.server_name}' completed")

    async def connect(self) -> None:
        """Connect to Streamable HTTP MCP server (starts background task)."""
        if self._connection_task is not None:
            return  # Already connected

        # Check circuit breaker
        if self._circuit_breaker.is_open():
            logger.warning(f"Circuit breaker is OPEN for '{self.server_name}' - blocking connection attempt")
            raise RuntimeError(f"Circuit breaker is OPEN for server '{self.server_name}'")

        self._connection_attempts += 1

        # Reset coordination
        self._ready_event.clear()
        self._shutdown_event.clear()
        self._connection_error = None

        # Start background task
        self._connection_task = asyncio.create_task(
            self._connection_task_impl(),
            name=f"mcp-http-{self.server_name}"
        )

        # Wait for ready signal
        await self._ready_event.wait()

        # Check for startup errors
        if self._connection_error:
            await self._connection_task
            self._connection_task = None
            raise RuntimeError(f"Streamable HTTP MCP server connection failed: {self._connection_error}")

    async def _discover_capabilities(self) -> None:
        """Discover tools, resources, and prompts from server."""
        if not self.session:
            return

        # Discover tools
        tools_result = await self.session.list_tools()
        self.tools = [
            {
                "name": tool.name,
                "description": tool.description or "",
                "input_schema": tool.inputSchema,
            }
            for tool in tools_result.tools
        ]

        # Discover resources
        try:
            resources_result = await self.session.list_resources()
            self.resources = [
                {
                    "uri": str(resource.uri),
                    "name": resource.name,
                    "description": resource.description or "",
                    "mime_type": resource.mimeType if hasattr(resource, "mimeType") else None,
                }
                for resource in resources_result.resources
            ]
        except Exception as e:
            logger.debug(f"Server '{self.server_name}' does not support resources: {e}")
            self.resources = []

        # Discover prompts
        try:
            prompts_result = await self.session.list_prompts()
            self.prompts = [
                {
                    "name": prompt.name,
                    "description": prompt.description or "",
                    "arguments": [
                        {"name": arg.name, "description": arg.description or "", "required": arg.required}
                        for arg in (prompt.arguments or [])
                    ],
                }
                for prompt in prompts_result.prompts
            ]
        except Exception as e:
            logger.debug(f"Server '{self.server_name}' does not support prompts: {e}")
            self.prompts = []

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        """Call a tool on the MCP server."""
        if not self._connected or not self.session:
            await self.connect()

        try:
            result = await self.session.call_tool(tool_name, arguments=arguments)
            self._circuit_breaker.record_success()
            return result

        except Exception as e:
            self._circuit_breaker.record_failure()
            self._last_error = e
            logger.error(f"Tool call failed for '{tool_name}' on '{self.server_name}': {e}")
            raise RuntimeError(f"Tool execution failed: {e}") from e

    async def read_resource(self, uri: str) -> Any:
        """Read a resource from the MCP server."""
        if not self._connected or not self.session:
            await self.connect()

        try:
            result = await self.session.read_resource(uri=uri)
            self._circuit_breaker.record_success()
            return result

        except Exception as e:
            self._circuit_breaker.record_failure()
            self._last_error = e
            logger.error(f"Resource read failed for '{uri}' on '{self.server_name}': {e}")
            raise RuntimeError(f"Resource read failed: {e}") from e

    async def get_prompt(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        """Get a prompt from the MCP server."""
        if not self._connected or not self.session:
            await self.connect()

        try:
            result = await self.session.get_prompt(name=name, arguments=arguments or {})
            self._circuit_breaker.record_success()
            return result

        except Exception as e:
            self._circuit_breaker.record_failure()
            self._last_error = e
            logger.error(f"Get prompt failed for '{name}' on '{self.server_name}': {e}")
            raise RuntimeError(f"Get prompt failed: {e}") from e

    async def set_logging_level(self, level: str) -> None:
        """Set the logging level for the MCP server."""
        if not self._connected or not self.session:
            await self.connect()

        try:
            await self.session.set_logging_level(level=level)
            logger.info(f"Set logging level to '{level}' for server '{self.server_name}'")

        except Exception as e:
            logger.error(f"Failed to set logging level: {e}")
            raise

    async def disconnect(self) -> None:
        """Disconnect from Streamable HTTP MCP server (stops background task cleanly)."""
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
            logger.info(f"Disconnected from Streamable HTTP MCP server '{self.server_name}'")

    def get_tools(self) -> list[dict[str, Any]]:
        """Get the list of discovered tools."""
        return self.tools

    def get_resources(self) -> list[dict[str, Any]]:
        """Get the list of discovered resources."""
        return self.resources

    def get_prompts(self) -> list[dict[str, Any]]:
        """Get the list of discovered prompts."""
        return self.prompts
