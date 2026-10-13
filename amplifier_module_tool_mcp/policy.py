"""One exact-name authority for discovery and dispatch, independent of hints."""

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ToolPolicy:
    # None is legacy unrestricted; an empty set explicitly denies everything.
    names: frozenset[str] | None = None

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> "ToolPolicy":
        if "tools" not in config:
            return cls()
        names = config["tools"]
        if not isinstance(names, list) or any(
            not isinstance(name, str)
            or not name
            or name != name.strip()
            or any(char in name for char in "*?[]")
            for name in names
        ):
            raise ValueError(
                "MCP server 'tools' must be a list of exact nonempty names "
                "(no patterns or surrounding whitespace); [] denies all"
            )
        return cls(frozenset(names))

    def allows(self, name: str) -> bool:
        return self.names is None or name in self.names

    def require_tool(self, name: str) -> None:
        if not self.allows(name):
            raise PermissionError(f"MCP tool {name!r} is not approved by server tools policy")

    def require_auxiliary(self) -> None:
        if self.names is not None:
            raise PermissionError(
                "MCP resources and prompts are disabled on tool-restricted servers"
            )


def validate_server_policy(config: dict[str, Any]) -> ToolPolicy:
    """Reject misspelled/unsupported keys in the opted-in configuration."""
    policy = ToolPolicy.from_config(config)
    supported = {"tools", "type", "command", "args", "env", "url", "headers"}
    unknown = set(config) - supported
    # Do not silently treat a likely restriction typo as a legacy config.
    restriction_keys = {
        key for key in unknown
        if "tool" in key.lower() or key.lower() in {"permissions", "allowlist", "denylist"}
    }
    if (policy.names is not None and unknown) or restriction_keys:
        raise ValueError(f"Unsupported MCP server policy/config keys: {sorted(unknown)}")
    return policy