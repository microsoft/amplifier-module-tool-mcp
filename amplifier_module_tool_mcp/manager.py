"""MCP Manager that orchestrates multiple MCP clients and their capabilities."""

import asyncio
import logging
import os
from pathlib import Path
from typing import Any

from amplifier_module_tool_mcp.client import MCPClient
from amplifier_module_tool_mcp.config import MCPConfig
from amplifier_module_tool_mcp.content_utils import DEFAULT_MAX_CONTENT_SIZE
from amplifier_module_tool_mcp.prompt_wrapper import MCPPromptWrapper
from amplifier_module_tool_mcp.resource_wrapper import MCPResourceWrapper
from amplifier_module_tool_mcp.streamable_http_client import MCPStreamableHTTPClient
from amplifier_module_tool_mcp.wrapper import MCPToolWrapper

logger = logging.getLogger(__name__)


class MCPManager:
    """
    Manages multiple MCP servers and their capabilities.

    Responsibilities:
    - Load server configurations
    - Start/stop MCP clients (stdio and Streamable HTTP)
    - Discover tools, resources, and prompts from all servers
    - Create wrappers for all primitives
    - Provide unified registry for Amplifier
    """

    def __init__(self, config: dict[str, Any], coordinator: Any):
        """
        Initialize MCP manager.

        Args:
            config: Configuration dictionary (inline config from profile)
            coordinator: Amplifier coordinator instance for hooks
        """
        self.config = MCPConfig(config)
        self.coordinator = coordinator
        self.clients: dict[str, MCPClient | MCPStreamableHTTPClient] = {}
        self.tools: dict[str, MCPToolWrapper] = {}
        self.resources: dict[str, MCPResourceWrapper] = {}
        self.prompts: dict[str, MCPPromptWrapper] = {}

        # Verbose server output configuration
        # Default: suppress server stderr (quiet by default)
        # Can be overridden via config or environment variable
        self.verbose_servers = config.get("verbose_servers", False) or os.environ.get(
            "AMPLIFIER_MCP_VERBOSE", ""
        ).lower() in ("true", "1", "yes")

        # Where to save server logs when suppressed
        default_log_dir = "~/.amplifier/logs/mcp-servers/"
        self.server_log_dir = Path(
            config.get("server_log_dir", default_log_dir)
        ).expanduser()

        # Content size limit to prevent context exhaustion
        self.max_content_size = config.get("max_content_size", DEFAULT_MAX_CONTENT_SIZE)
        logger.debug(f"Content size limit: {self.max_content_size:,} characters")

    async def start(self) -> None:
        """
        Start all configured MCP servers and discover their capabilities.

        This:
        1. Loads server configurations from all sources
        2. Creates MCP clients for each server (stdio or Streamable HTTP)
        3. Connects to servers and discovers capabilities
        4. Wraps tools, resources, and prompts for Amplifier
        """
        logger.info("Starting MCP manager...")

        # Load server configurations
        servers = self.config.load_servers()

        if not servers:
            logger.debug("No MCP servers configured - MCP capabilities inactive")
            return

        # Start each server
        for server_name, server_config in servers.items():
            try:
                await self._start_server(server_name, server_config)
            except Exception as e:
                logger.error(f"Failed to start MCP server '{server_name}': {e}")
                # Continue with other servers

        logger.info(
            f"MCP manager started: {len(self.tools)} tools, {len(self.resources)} resources, "
            f"{len(self.prompts)} prompts from {len(self.clients)} servers"
        )

    async def _start_server(
        self, server_name: str, server_config: dict[str, Any]
    ) -> None:
        """
        Start a single MCP server and discover its capabilities.

        Args:
            server_name: Unique name for this server
            server_config: Server configuration
        """
        # Detect transport type
        transport_type = self._detect_transport_type(server_config)

        if transport_type == "streamable-http":
            await self._start_streamable_http_server(server_name, server_config)
        elif transport_type == "stdio":
            await self._start_stdio_server(server_name, server_config)
        else:
            logger.error(
                f"Unknown transport type '{transport_type}' for server '{server_name}'"
            )

    def _detect_transport_type(self, server_config: dict[str, Any]) -> str:
        """
        Detect transport type from server configuration.

        Args:
            server_config: Server configuration

        Returns:
            "streamable-http" or "stdio"
        """
        # Explicit type in config
        if "type" in server_config:
            transport = server_config["type"]
            # Normalize variations
            if transport in (
                "streamable-http",
                "streamable_http",
                "streamablehttp",
                "http",
            ):
                return "streamable-http"
            return transport

        # URL means Streamable HTTP (current spec)
        if "url" in server_config:
            return "streamable-http"

        # Command means stdio
        if "command" in server_config:
            return "stdio"

        # Default to stdio
        return "stdio"

    async def _start_stdio_server(
        self, server_name: str, server_config: dict[str, Any]
    ) -> None:
        """Start a stdio-based MCP server."""
        command = server_config.get("command")
        args = server_config.get("args", [])
        env = server_config.get("env", {})

        if not command:
            logger.error(
                f"Stdio server '{server_name}' missing 'command' in configuration"
            )
            return

        # Substitute environment variables
        env = {k: MCPConfig.substitute_env_vars(v) for k, v in env.items()}

        # Create and connect client with verbose settings
        client = MCPClient(
            server_name,
            command,
            args,
            env,
            verbose_servers=self.verbose_servers,
            server_log_dir=self.server_log_dir,
        )
        await client.connect()

        # Store client and register capabilities
        self.clients[server_name] = client
        await self._register_capabilities(server_name, client)

    async def _start_streamable_http_server(
        self, server_name: str, server_config: dict[str, Any]
    ) -> None:
        """Start a Streamable HTTP-based MCP server (2025-03-26 spec)."""
        url = server_config.get("url")
        headers = server_config.get("headers", {})

        if not url:
            logger.error(
                f"Streamable HTTP server '{server_name}' missing 'url' in configuration"
            )
            return

        # Substitute environment variables in headers
        headers = {k: MCPConfig.substitute_env_vars(v) for k, v in headers.items()}

        # Create and connect client
        client = MCPStreamableHTTPClient(server_name, url, headers)
        await client.connect()

        # Store client and register capabilities
        self.clients[server_name] = client
        await self._register_capabilities(server_name, client)

    async def _register_capabilities(
        self, server_name: str, client: MCPClient | MCPStreamableHTTPClient
    ) -> None:
        """
        Register all capabilities (tools, resources, prompts) from a client.

        Args:
            server_name: Server name
            client: Connected MCP client
        """
        # Register tools - pass hooks for event emission and content size limit
        for tool_def in client.get_tools():
            wrapper = MCPToolWrapper(
                server_name,
                tool_def,
                client,
                self.coordinator.hooks,
                max_content_size=self.max_content_size,
            )
            self.tools[wrapper.name] = wrapper
            logger.debug(
                f"Registered tool '{wrapper.name}' from server '{server_name}'"
            )

        # Register resources - pass hooks for event emission and content size limit
        for resource_def in client.get_resources():
            wrapper = MCPResourceWrapper(
                server_name,
                resource_def,
                client,
                self.coordinator.hooks,
                max_content_size=self.max_content_size,
            )
            self.resources[wrapper.name] = wrapper
            logger.debug(
                f"Registered resource '{wrapper.name}' from server '{server_name}'"
            )

        # Register prompts - pass hooks for event emission and content size limit
        for prompt_def in client.get_prompts():
            wrapper = MCPPromptWrapper(
                server_name,
                prompt_def,
                client,
                self.coordinator.hooks,
                max_content_size=self.max_content_size,
            )
            self.prompts[wrapper.name] = wrapper
            logger.debug(
                f"Registered prompt '{wrapper.name}' from server '{server_name}'"
            )

    async def stop(self) -> None:
        """Stop all MCP servers cleanly."""
        logger.info("Stopping MCP manager...")

        # Disconnect all servers in parallel
        await asyncio.gather(
            *[client.disconnect() for client in self.clients.values()],
            return_exceptions=True,
        )

        self.clients.clear()
        self.tools.clear()
        self.resources.clear()
        self.prompts.clear()

        logger.info("MCP manager stopped")

    def get_tools(self) -> dict[str, MCPToolWrapper]:
        """Get all registered tools."""
        return self.tools

    def get_resources(self) -> dict[str, MCPResourceWrapper]:
        """Get all registered resources."""
        return self.resources

    def get_prompts(self) -> dict[str, MCPPromptWrapper]:
        """Get all registered prompts."""
        return self.prompts

    def get_all_capabilities(self) -> dict[str, Any]:
        """
        Get all capabilities (tools + resources + prompts).

        Returns:
            Dictionary with all wrappers
        """
        return {**self.tools, **self.resources, **self.prompts}

    def get_server_names(self) -> list[str]:
        """Get list of connected server names."""
        return list(self.clients.keys())
