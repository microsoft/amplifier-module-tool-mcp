"""Configuration loading for MCP servers."""

import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class MCPServerConfig(dict[str, Any]):
    """Server configuration with trusted loader provenance."""

    def __init__(self, config: dict[str, Any], source: str):
        super().__init__(config)
        self.source = source


class MCPConfig:
    """
    Handles loading and resolution of MCP server configuration.

    Configuration can come from multiple sources (in priority order):
    1. Inline config (passed directly to module)
    2. Absolute environment-selected config outside the project
    3. User config (~/.amplifier/mcp.json) - personal defaults

    Project files are not a source of server authority, for either transport.
    Inline configuration must be supplied by a trusted host, not a project bundle.

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
        # Freeze host locators and retain the original project boundary even if
        # the caller changes cwd before starting (or restarting) the manager.
        self._home = Path.home().resolve()
        self._project_roots: set[Path] = set()
        self._remember_project()
        self._env_path = os.environ.get("AMPLIFIER_MCP_CONFIG")

    def _remember_project(self) -> None:
        """Remember cwd's project root without running git or reading config."""
        cwd = Path.cwd().resolve()
        # Running from HOME is not project discovery. Its user config remains
        # usable, but a real checkout rooted at HOME still counts as a project.
        config_root = None
        for directory in (cwd, *cwd.parents):
            if (directory / ".git").exists():
                self._project_roots.add(directory)
                return
            if directory != self._home and (directory / ".amplifier").is_dir():
                config_root = directory
            if directory == self._home:
                break
        if cwd != self._home:
            self._project_roots.add(config_root or cwd)

    def load_servers(self) -> dict[str, MCPServerConfig]:
        """
        Load MCP server configurations from all sources.

        Returns:
            Dictionary mapping server names to their configurations
        """
        servers: dict[str, MCPServerConfig] = {}
        self._remember_project()

        # Load from each source (reverse priority order, so higher priority overwrites)
        # Project entries cannot shadow host-owned definitions.
        for source, config in [
            ("user", self._load_from_user_config()),
            ("environment", self._load_from_env()),
            ("inline", self._load_from_inline()),
        ]:
            if config and "mcpServers" in config:
                if not isinstance(config["mcpServers"], dict):
                    logger.warning(
                        "Ignoring %s MCP config: mcpServers must be an object", source
                    )
                    continue
                for server_name, server_config in config["mcpServers"].items():
                    if not isinstance(server_config, dict):
                        logger.warning(
                            "Ignoring MCP server '%s' from %s config: "
                            "configuration must be an object",
                            server_name,
                            source,
                        )
                        continue

                    servers[server_name] = MCPServerConfig(server_config, source)

        logger.info(f"Loaded {len(servers)} MCP server configurations")
        return servers

    def _load_from_inline(self) -> dict[str, Any] | None:
        """Load configuration from inline config (profile)."""
        if "servers" in self.inline_config:
            logger.debug("Loading MCP servers from inline config")
            return {"mcpServers": self.inline_config["servers"]}
        return None

    def _load_from_user_config(self) -> dict[str, Any] | None:
        """Load configuration from ~/.amplifier/mcp.json."""
        return self._load_host_file(self._home / ".amplifier" / "mcp.json", "user")

    def _load_from_env(self) -> dict[str, Any] | None:
        """Load an absolute host-owned config, never a cwd-relative locator."""
        if self._env_path:
            return self._load_host_file(Path(self._env_path), "environment")
        return None

    def _load_host_file(self, path: Path, source: str) -> dict[str, Any] | None:
        """Validate the locator AND its canonical target before reading content.

        Host-selected files outside project roots are authority. Symlinks into
        a project and hard-linked files are not a migration to host ownership.
        This is not a sandbox against another process running as the host user.
        """
        try:
            if not path.is_absolute():
                raise ValueError("configuration path must be absolute")
            canonical = self._resolve_host_path(path)
            stat = canonical.stat()
            if not canonical.is_file() or stat.st_nlink != 1:
                raise ValueError(
                    "configuration must be a regular, non-hard-linked file"
                )
            if os.name == "posix" and stat.st_uid not in (0, os.getuid()):
                raise ValueError("configuration must be owned by the host user or root")
            return self._load_json_file(canonical, source)
        except FileNotFoundError:
            return None
        except (OSError, RuntimeError, ValueError) as exc:
            logger.warning("Ignoring %s MCP config at %s: %s", source, path, exc)
            return None

    def _resolve_host_path(self, path: Path) -> Path:
        """Check symlink hops too, not just the final canonical destination.

        A host alias -> project link -> host file still depends on a project
        controlled selector and must not grant authority to that selector.
        """
        pending = [path]
        checked: set[Path] = set()
        canonical = path.resolve(strict=True)
        while pending:
            candidate = pending.pop()
            if candidate in checked:
                continue
            checked.add(candidate)
            resolved = candidate.resolve(strict=True)
            if any(
                location.is_relative_to(root)
                for root in self._project_roots
                for location in (candidate, Path(os.path.abspath(candidate)), resolved)
            ):
                raise ValueError("configuration must be outside project roots")
            for prefix in (candidate, *candidate.parents):
                if prefix.is_symlink():
                    target = prefix.readlink()
                    pending.append(
                        target if target.is_absolute() else prefix.parent / target
                    )
        return canonical

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
            if not isinstance(config, dict):
                logger.warning("Ignoring %s MCP config: root must be an object", source)
                return None
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
