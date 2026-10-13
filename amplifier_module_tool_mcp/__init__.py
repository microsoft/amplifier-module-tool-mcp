"""
MCP (Model Context Protocol) Tool Module for Amplifier.

This module enables Amplifier to connect to MCP servers and expose their
capabilities (tools, resources, prompts) to LLM agents through the standard
Amplifier interface.
"""

# Amplifier module metadata
__amplifier_module_type__ = "tool"

import logging
from typing import Any

from amplifier_core import ModuleCoordinator

from amplifier_module_tool_mcp.manager import MCPManager, NamespaceCollisionError

__version__ = "0.2.2"
__all__ = ["mount", "MCPManager"]

logger = logging.getLogger(__name__)


async def mount(coordinator: ModuleCoordinator, config: dict[str, Any] | None = None):
    """
    Mount the MCP tool module.

    This is the entry point called by Amplifier when loading this module.

    Args:
        coordinator: Amplifier coordinator instance
        config: Configuration dictionary with optional 'servers' key

    Returns:
        Optional cleanup function
    """
    config = config or {}
    expose_tools = config.get("expose_tools", True)
    if type(expose_tools) is not bool:
        raise ValueError("expose_tools must be a bool")
    visibility_config = config.get("visibility", {})
    if not isinstance(visibility_config, dict):
        raise ValueError("visibility must be a dict")
    # This port belongs to one mount. Never replace another mount's handles,
    # even if its envelope looks compatible.
    if coordinator.get_capability("mcp.backends.v1") is not None:
        raise NamespaceCollisionError("Core capability mcp.backends.v1 already registered")

    manager = MCPManager(config, coordinator)
    try:
        await manager.start()
        capabilities = manager.get_all_capabilities()
        existing = coordinator.get("tools") or {}
        collisions = set(capabilities).intersection(existing)
        if collisions:
            raise NamespaceCollisionError(f"MCP names already mounted: {sorted(collisions)}")

        if expose_tools:
            for cap_name, cap_wrapper in capabilities.items():
                await coordinator.mount("tools", cap_wrapper, name=cap_name)
                logger.debug(f"Mounted MCP capability: {cap_name}")

        tools = manager.get_tools()
        resources = manager.get_resources()
        prompts = manager.get_prompts()
        server_names = manager.get_server_names()
        if expose_tools and visibility_config.get("enabled", True) and server_names:
            from amplifier_module_tool_mcp.hooks import MCPVisibilityHook

            hook = MCPVisibilityHook(manager, visibility_config)
            coordinator.hooks.register(
                event="provider:request",
                handler=hook.on_provider_request,
                priority=hook.priority,
                name="mcp-visibility",
            )
            logger.info(f"Mounted MCP visibility hook with {len(server_names)} servers")

        async def cleanup():
            logger.info("Cleaning up MCP module...")
            await manager.stop()

        logger.info(
            f"MCP module mounted: {len(tools)} tools, {len(resources)} resources, "
            f"{len(prompts)} prompts from {len(server_names)} servers"
        )
        # Publish only after the remaining fallible setup. Core registrations
        # are not transactional: failure may leave earlier tools/hooks, whose
        # clients are retired below so retained handles cannot reconnect/RPC.
        coordinator.register_capability(
            "mcp.backends.v1",
            {"exposed": expose_tools, "capabilities": capabilities},
        )
        return cleanup
    except BaseException:
        try:
            await manager.stop()
        except BaseException:
            logger.exception("MCP cleanup failed while unwinding mount")
        raise
