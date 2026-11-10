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

from amplifier_module_tool_mcp.manager import MCPManager

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
    ephemeral = coordinator.get_capability("mcp.ephemeral_servers")
    coordinator.register_capability("mcp.ephemeral_servers", None)
    coordinator.register_capability("mcp.ephemeral_ready", None)
    manager = MCPManager(config or {}, coordinator)
    mounted = {}

    async def cleanup():
        coordinator.register_capability("mcp.ephemeral_ready", None)
        try:
            for name, wrapper in mounted.items():
                if coordinator.get("tools", name) is wrapper:
                    await coordinator.unmount("tools", name)
        finally:
            mounted.clear()
            await manager.stop()

    try:
        await manager.start(ephemeral_servers=ephemeral)
        # Register all capabilities with the coordinator
        capabilities = manager.get_all_capabilities()

        if capabilities.keys() & (coordinator.get("tools") or {}).keys():
            raise ValueError("MCP capabilities conflict with mounted tools")

        for cap_name, cap_wrapper in capabilities.items():
            mounted[cap_name] = cap_wrapper
            await coordinator.mount("tools", cap_wrapper, name=cap_name)
            logger.debug(f"Mounted MCP capability: {cap_name}")

        # Log summary
        tools = manager.get_tools()
        resources = manager.get_resources()
        prompts = manager.get_prompts()

        logger.info(
            f"MCP module mounted: {len(tools)} tools, {len(resources)} resources, "
            f"{len(prompts)} prompts from {len(manager.get_server_names())} servers"
        )

        # Mount MCP visibility hook if enabled AND servers exist
        # Skip registration entirely when no servers - no point running a hook that does nothing
        visibility_config = (config or {}).get("visibility", {})
        server_names = manager.get_server_names()
        if visibility_config.get("enabled", True) and server_names:
            from amplifier_module_tool_mcp.hooks import MCPVisibilityHook

            hook = MCPVisibilityHook(manager, visibility_config)

            # Register hook on provider:request event
            coordinator.hooks.register(
                event="provider:request",
                handler=hook.on_provider_request,
                priority=hook.priority,
                name="mcp-visibility",
            )

            logger.info(f"Mounted MCP visibility hook with {len(server_names)} servers")

        if ephemeral is not None:
            coordinator.register_capability("mcp.ephemeral_ready", sorted(ephemeral))
    except BaseException:
        await cleanup()
        raise

    return cleanup
