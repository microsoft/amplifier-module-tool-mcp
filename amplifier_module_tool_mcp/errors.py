"""Error-shape helpers shared by the MCP transports."""

__all__ = ["extract_root_cause"]


def extract_root_cause(exc: BaseException) -> BaseException:
    """Unwrap ExceptionGroup to the most informative root cause.

    The MCP SDK's anyio TaskGroup surfaces errors as
    ExceptionGroup("unhandled errors in a TaskGroup", [actual_error]).
    ``str()`` on that group omits the sub-exceptions entirely, so logging it
    directly hides the real failure. Unwrapping gives callers the actual cause
    (e.g. an HTTP 502, or an AttributeError from an SDK field rename).

    Available natively from Python 3.11; anyio produces ExceptionGroup on
    earlier versions too via the exceptiongroup backport.
    """
    if isinstance(exc, ExceptionGroup) and exc.exceptions:
        return extract_root_cause(exc.exceptions[0])
    return exc
