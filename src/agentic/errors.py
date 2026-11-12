"""Structured exception description for the audit log.

`str(exc)` is useless for the two things that actually break an async loop:
  * asyncio.TaskGroup wraps failures in an ExceptionGroup whose str() is only
    "unhandled errors in a TaskGroup (1 sub-exception)" — the real cause is hidden.
  * a re-raised error's root cause lives on __cause__ / __context__, not in str().

`describe_exception` unwraps both so the ERROR audit payload carries the real type,
message, and a short traceback tail — diagnosable from /api/audit alone, without
needing container stdout (which isn't reachable on the managed VPS).
"""
from __future__ import annotations

import traceback
from typing import Any

_MAX_TB_CHARS = 3000
_MAX_LEAVES = 5


def _leaf_exceptions(exc: BaseException, _depth: int = 0) -> list[BaseException]:
    """Flatten an ExceptionGroup (recursively) to its non-group leaf exceptions.

    A plain exception is its own only leaf. Depth-guarded against pathological nesting.
    """
    subs = getattr(exc, "exceptions", None)  # ExceptionGroup / BaseExceptionGroup
    if not subs or _depth > 5:
        return [exc]
    out: list[BaseException] = []
    for sub in subs:
        out.extend(_leaf_exceptions(sub, _depth + 1))
    return out


def _root(exc: BaseException) -> BaseException:
    """Follow __cause__ / __context__ to the underlying error (bounded)."""
    seen: set[int] = set()
    cur = exc
    for _ in range(10):
        nxt = cur.__cause__ or cur.__context__
        if nxt is None or id(nxt) in seen:
            break
        seen.add(id(cur))
        cur = nxt
    return cur


def describe_exception(exc: BaseException) -> dict[str, Any]:
    """A structured, audit-friendly description of ``exc``.

    Returns keys:
      error       — str(exc): the top-level (possibly masked) message, kept for continuity.
      type        — the top-level exception class name.
      root_causes — the real leaf error(s): "ClassName: message", ExceptionGroup unwrapped.
      traceback   — the tail of the first leaf's formatted traceback (truncated).
    """
    leaves = _leaf_exceptions(exc)
    roots = [_root(leaf) for leaf in leaves]
    root_causes = [f"{type(r).__name__}: {r}" for r in roots[:_MAX_LEAVES]]
    if len(roots) > _MAX_LEAVES:
        root_causes.append(f"... (+{len(roots) - _MAX_LEAVES} more)")

    tb_src = roots[0] if roots else exc
    tb = "".join(
        traceback.format_exception(type(tb_src), tb_src, tb_src.__traceback__)
    )
    if len(tb) > _MAX_TB_CHARS:
        tb = "...(truncated)...\n" + tb[-_MAX_TB_CHARS:]

    return {
        "error": str(exc),
        "type": type(exc).__name__,
        "root_causes": root_causes,
        "traceback": tb,
    }


# Raised error, a group leaf, or an explicit __cause__. Not implicit __context__.
_TRANSIENT_CONNECT = (
    ("httpx", "ConnectError"),
    ("httpcore", "ConnectError"),
    ("anyio", "ClosedResourceError"),
)


def _matches_transient_connect(exc: BaseException) -> bool:
    for cls in type(exc).mro():
        module = getattr(cls, "__module__", "") or ""
        name = cls.__name__
        for prefix, type_name in _TRANSIENT_CONNECT:
            if name == type_name and (module == prefix or module.startswith(prefix + ".")):
                return True
    return False


def _raised_connect_nodes(exc: BaseException, seen: set[int], depth: int = 0):
    """The raised error, group leaves, and explicit causes. Suppressed context is skipped."""
    if id(exc) in seen or depth > 12:
        return
    seen.add(id(exc))
    yield exc
    if isinstance(exc, BaseExceptionGroup):
        for sub in exc.exceptions:
            yield from _raised_connect_nodes(sub, seen, depth + 1)
    cause = exc.__cause__
    if cause is not None:
        yield from _raised_connect_nodes(cause, seen, depth + 1)


def is_transient_connect_error(exc: BaseException) -> bool:
    """True when this raised failure itself is a transient connect error.

    A ``ValueError`` raised ``from None`` while handling a ConnectError is not one:
    the ConnectError is only suppressed context.
    """
    return any(_matches_transient_connect(node) for node in _raised_connect_nodes(exc, set()))
