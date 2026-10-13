"""Bounded presentation with exact recovery of the SDK-parsed JSON envelope.

Recovery is not a wire-byte capture: the SDK may normalize protocol fields.
No service status inference or sensitivity-label enforcement happens here.
"""

import hashlib
import json
import os
import secrets
import stat
from copy import deepcopy
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any


DEFAULT_ARTIFACT_DIR = "~/.amplifier/artifacts/mcp"


def json_value(value: Any) -> Any:
    """Serialize SDK models by protocol alias; support legacy test doubles too."""
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", by_alias=True, exclude_none=True)
    if is_dataclass(value):
        return json_value(asdict(value))
    if isinstance(value, dict):
        return {key: json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_value(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    fields = dict(vars(value))
    # Legacy callers/tests supplied plain objects with class-level attributes.
    for name in ("text", "type", "content", "structured_content", "structuredContent",
                 "is_error", "isError", "meta", "annotations"):
        if hasattr(value, name):
            fields[name] = getattr(value, name)
    if not fields:
        raise TypeError(f"Unsupported MCP evidence shape: {type(value).__name__}")
    return json_value(fields)


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def validate_budget(max_size: int) -> None:
    if type(max_size) is not int or max_size < 1024:
        raise ValueError("MCP max_content_size must be an integer >= 1024")


class ArtifactCreationError(RuntimeError):
    """Evidence was not recoverable; never report this as a successful preview."""


def save_envelope(envelope: Any, artifact_dir: Path) -> dict[str, Any]:
    data = canonical_json(envelope).encode("utf-8")
    name = secrets.token_hex(24) + ".json"
    if os.name == "nt":
        from amplifier_module_tool_mcp.windows_artifacts import save_private_bytes

        try:
            path = save_private_bytes(data, artifact_dir, name)
        except Exception as exc:
            raise ArtifactCreationError(f"MCP recovery artifact creation failed: {exc}") from exc
        return recovery_metadata(path, data)
    if os.name != "posix":
        raise ArtifactCreationError(f"MCP private recovery unsupported on platform {os.name}")
    directory = artifact_dir.expanduser().absolute()
    directory_fd = None
    created = False
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        # Do not follow a symlink or silently change another directory's mode.
        directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        info = os.fstat(directory_fd)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise OSError("artifact directory must be owned by this user with mode 0700")
        fd = os.open(
            name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600, dir_fd=directory_fd,
        )
        created = True
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        return recovery_metadata(str(directory / name), data)
    except (OSError, ValueError) as exc:
        if created and directory_fd is not None:
            os.unlink(name, dir_fd=directory_fd)
        raise ArtifactCreationError(f"MCP recovery artifact creation failed: {exc}") from exc
    finally:
        if directory_fd is not None:
            os.close(directory_fd)


def recovery_metadata(path: str, data: bytes) -> dict[str, Any]:
    return {
        "path": path, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
        "complete": True, "format": "mcp-sdk-envelope-json",
        "lifetime": "until explicitly deleted by host",
    }


def compact_envelope(envelope: Any, text: str, structured: Any) -> tuple[Any, list]:
    """Reference duplicated values, retaining exact reconstruction information.

    Only deduplicate known equal fields; unknown/nontext shapes stay untouched.
    References live outside the source envelope, so SDK extension fields cannot
    be mistaken for adapter markers.
    """
    compact = deepcopy(envelope)
    references = []
    if not isinstance(compact, dict):
        return compact, references
    items = compact.get("content")
    if (
        isinstance(items, list) and items
        and all(isinstance(item, dict) and isinstance(item.get("text"), str) for item in items)
        and "\n".join(item["text"] for item in items) == text
    ):
        start = 0
        for index, item in enumerate(items):
            value = item.pop("text")
            references.append({
                "path": ["content", index, "text"], "source": "content",
                "start": start, "length": len(value),
            })
            start += len(value) + 1
    for key in ("structuredContent", "structured_content"):
        if key in compact and canonical_json(compact[key]) == canonical_json(structured):
            del compact[key]
            references.append({"path": [key], "source": "structured_content"})
    return compact, references


def present_evidence(
    envelope: Any, text: str, structured: Any, max_size: int, artifact_dir: Path,
) -> dict[str, Any]:
    """Evidence fields share one character budget; bookkeeping is additional."""
    validate_budget(max_size)
    envelope = json_value(envelope)
    structured = json_value(structured)
    structured_json = canonical_json(structured) if structured is not None else ""
    total = len(text) + len(structured_json) + len(canonical_json(envelope))
    if total <= max_size:
        return {
            "content": text, "structured_content": structured, "mcp_envelope": envelope,
            "content_size_chars": len(text), "content_truncated": False,
            "evidence_complete": True,
        }
    compact, references = compact_envelope(envelope, text, structured)
    compact_size = len(text) + len(structured_json) + len(canonical_json(compact))
    compact_size += len(canonical_json(references))
    if references and compact_size <= max_size:
        return {
            "content": text, "structured_content": structured, "mcp_envelope": compact,
            "mcp_envelope_references": references,
            "content_size_chars": len(text), "content_truncated": False,
            "evidence_complete": True,
        }
    recovery = save_envelope(envelope, artifact_dir)
    # Leave room for complementary structured evidence even when text is long.
    preview = text[:max_size // 2] if structured_json else text[:max_size]
    remaining = max_size - len(preview)
    structured_fits = len(structured_json) <= remaining
    output = {
        "content": preview,
        "structured_content": structured if structured_fits else None,
        "mcp_envelope": None,
        "content_size_chars": len(preview),
        "content_truncated": True,
        "evidence_complete": False,
        "recovery": recovery,
    }
    if not structured_fits:
        # Explicit serialized excerpt, never masquerades as a complete object.
        output["structured_preview"] = structured_json[:remaining]
    return output