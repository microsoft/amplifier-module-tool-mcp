"""Configuration loading for MCP servers."""

import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class MCPConfig:
    """
    Handles loading and resolution of MCP server configuration.

    Configuration can come from multiple sources (in priority order):
    1. Inline config (passed directly to module)
    2. Environment variable (AMPLIFIER_MCP_CONFIG) - explicit override
    3. Project config (.amplifier/mcp.json)
    4. User config (~/.amplifier/mcp.json) - personal defaults

    This follows amplifier-foundation conventions where env vars are
    overrides (high priority), not fallbacks.
    """

    def __init__(self, inline_config: dict[str, Any] | None = None):
        """
        Initialize configuration loader.

        Args:
            inline_config: Optional inline configuration from profile
        """
        self.inline_config = inline_config or {}

    def load_servers(self) -> dict[str, dict[str, Any]]:
        """
        Load MCP server configurations from all sources.

        Returns:
            Dictionary mapping server names to their configurations
        """
        servers = {}

        # Load from each source (reverse priority order, so higher priority overwrites)
        # Priority: inline > env > project > user (matches foundation conventions)
        for config in [
            self._load_from_user_config(),
            self._load_from_project_config(),
            self._load_from_env(),
            self._load_from_inline(),
        ]:
            if config and "mcpServers" in config:
                servers.update(config["mcpServers"])

        logger.info(f"Loaded {len(servers)} MCP server configurations")
        return servers

    def _load_from_inline(self) -> dict[str, Any] | None:
        """Load configuration from inline config (profile)."""
        if "servers" in self.inline_config:
            logger.debug("Loading MCP servers from inline config")
            return {"mcpServers": self.inline_config["servers"]}
        return None

    def _load_from_project_config(self) -> dict[str, Any] | None:
        """Load configuration from .amplifier/mcp.json in current directory."""
        config_path = Path.cwd() / ".amplifier" / "mcp.json"
        return self._load_json_file(config_path, "project")

    def _load_from_user_config(self) -> dict[str, Any] | None:
        """Load configuration from ~/.amplifier/mcp.json."""
        config_path = Path.home() / ".amplifier" / "mcp.json"
        return self._load_json_file(config_path, "user")

    def _load_from_env(self) -> dict[str, Any] | None:
        """Load configuration from environment variable."""
        config_path_str = os.environ.get("AMPLIFIER_MCP_CONFIG")
        if config_path_str:
            config_path = Path(config_path_str)
            return self._load_json_file(config_path, "environment")
        return None

    def _load_json_file(self, path: Path, source: str) -> dict[str, Any] | None:
        """
        Load and parse a JSON configuration file.

        Args:
            path: Path to JSON file
            source: Source name for logging

        Returns:
            Parsed JSON or None if file doesn't exist or is invalid
        """
        if not path.exists():
            logger.debug(f"No {source} config at {path}")
            return None

        try:
            # encoding="utf-8-sig": a Windows-authored mcp.json (Notepad's
            # default "UTF-8" save) carries a BOM; read as plain utf-8 the
            # leading U+FEFF makes json.load raise, and the broad except below
            # silently drops ALL configured servers. utf-8-sig strips a leading
            # BOM if present and is identical to utf-8 otherwise.
            with open(path, encoding="utf-8-sig") as f:
                config = json.load(f)
            logger.debug(f"Loaded MCP config from {source}: {path}")
            return config
        except Exception as e:
            logger.warning(f"Failed to load {source} config from {path}: {e}")
            return None

    @staticmethod
    def substitute_env_vars(value: str) -> str:
        """
        Substitute environment variables in configuration values.

        Supports ${VAR} and ${VAR:-default} syntax.

        Args:
            value: String potentially containing env var references

        Returns:
            String with env vars substituted
        """
        if not isinstance(value, str):
            return value

        # Simple substitution for ${VAR} and ${VAR:-default}
        import re

        def replace_var(match: re.Match[str]) -> str:
            var_expr = match.group(1)
            if ":-" in var_expr:
                var_name, default = var_expr.split(":-", 1)
                return os.environ.get(var_name, default)
            return os.environ.get(var_expr) or ""

        return re.sub(r"\$\{([^}]+)\}", replace_var, value)
