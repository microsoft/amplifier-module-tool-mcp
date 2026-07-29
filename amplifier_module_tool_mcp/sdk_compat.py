"""Compatibility helpers for reading fields renamed across `mcp` SDK majors.

The `mcp` SDK renamed several Pydantic model fields from camelCase to
snake_case between the 1.x and 2.x major releases (e.g. ``Tool.inputSchema``
-> ``Tool.input_schema``, ``Resource.mimeType`` -> ``Resource.mime_type``).
This module lets callers read those fields without caring which SDK major
is installed.

Design constraint -- fail loud, no silent fallbacks:

A prior version of this code used patterns like::

    resource.mimeType if hasattr(resource, "mimeType") else None

On the SDK version where the field was renamed, ``hasattr`` evaluates to
``False`` and the expression silently returns ``None`` -- even though the
object legitimately carries the data under its new name. That is data loss
disguised as a missing-value case, and it produces no error, log, or any
other signal that something is wrong. The bug this module fixes was exactly
that pattern.

``sdk_field`` instead:

- Returns the value under the FIRST candidate name that exists on the
  object, using ``hasattr`` (not truthiness) to decide "exists" -- so a
  field that legitimately holds ``None`` (e.g. a resource with no MIME
  type) is still returned as ``None``, not treated as absent.
- Raises ``AttributeError`` when NONE of the candidate names exist. An
  object that doesn't have any of the known names is an unrecognized SDK
  shape, not a missing-value case, and must announce itself loudly rather
  than being papered over with a default.
"""

from __future__ import annotations

from typing import Any


def sdk_field(obj: Any, *names: str) -> Any:
    """Read a field from ``obj`` that the `mcp` SDK has renamed across majors.

    Tries each name in ``names`` in order and returns the value of the first
    one that exists on ``obj`` (existence checked via ``hasattr``, so a
    present-but-``None`` value is returned as-is, not skipped).

    Args:
        obj: The SDK model instance to read from (e.g. a ``Tool``,
            ``Resource``, or ``ResourceTemplate`` instance).
        *names: Candidate attribute names to try, in priority order (e.g.
            ``"input_schema", "inputSchema"``).

    Returns:
        The value of the first attribute name that exists on ``obj``. May
        legitimately be ``None`` if that attribute's value is ``None``.

    Raises:
        AttributeError: If none of ``names`` exist on ``obj``. Names at
            least one name that was attempted, and the object's type, so
            the failure is self-describing.

    Example:
        >>> sdk_field(tool, "input_schema", "inputSchema")
        {'type': 'object', 'properties': {...}}
    """
    if not names:
        raise ValueError("sdk_field() requires at least one candidate name")

    for name in names:
        if hasattr(obj, name):
            return getattr(obj, name)

    raise AttributeError(
        f"{type(obj).__name__!r} object has none of the expected attributes: "
        f"{', '.join(names)}. This likely means the installed `mcp` SDK "
        f"version uses a field shape this module does not yet know about."
    )


def extract_root_cause(exc: BaseException) -> BaseException:
    """Unwrap ExceptionGroup to the most informative root cause.

    The MCP SDK's anyio TaskGroup surfaces transport errors as
    ExceptionGroup("unhandled errors in a TaskGroup", [actual_error]).
    Unwrapping gives callers the real cause (e.g. HTTPStatusError 502)
    instead of the opaque ExceptionGroup string.

    Available natively from Python 3.11; anyio produces ExceptionGroup
    on earlier versions too via the exceptiongroup backport.
    """
    if isinstance(exc, ExceptionGroup) and exc.exceptions:
        return extract_root_cause(exc.exceptions[0])
    return exc
