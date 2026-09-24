"""Run one evaluation in a child process under a hard wall-clock timeout.

Two reasons this is not a plain function call:

- **Cost.** The engine builds a dependency graph over the whole workbook. The
  formula-cell gate in ``evaluate`` bounds the *input*, but not the time a
  pathological dependency graph takes to solve. Only a timeout bounds that, and
  a timeout is only enforceable against something you can kill.
- **Trust.** Evaluating a submitted workbook is executing caller-supplied logic.
  A child process means a crash, a memory blow-up or a hang costs one request
  rather than the whole service.

``signal.alarm`` would be the cheap alternative and does not work here: FastAPI
runs ``def`` path operations in its threadpool, and signals are only deliverable
on the main thread.

The child returns a tagged ``("ok", result)`` / ``("error", kind, payload)`` tuple
rather than raising through the pipe: ``UnsupportedFunctionError`` takes a list in
its constructor, and exceptions with non-trivial ``__init__`` signatures do not
survive pickling reliably.
"""

from __future__ import annotations

import multiprocessing as mp
from typing import Any

from evaluate import (
    EvaluationError,
    EvaluationResult,
    UnsupportedFunctionError,
    evaluate_workbook,
)


class EvaluationTimeoutError(Exception):
    """The evaluation exceeded its wall-clock budget and the child was killed."""


def _apply_memory_ceiling(max_memory_bytes: int | None) -> None:
    """Cap the child's address space so an allocation blow-up cannot take the container.

    Without this the "one request, not the container" claim is not actually
    enforced: ``MAX_FORMULA_CELLS`` bounds how many formulas there are, not how
    much each one allocates. A handful of formulas over full-column ranges
    (``=SUM(A1:A1048576)``, ``SUMPRODUCT`` over the same) can materialise
    million-row arrays and exhaust memory well inside the wall-clock budget. With
    one uvicorn worker that OOM would take down every tenant the instance is
    serving.

    ``RLIMIT_AS`` makes the allocation fail inside the child instead. The child
    dies without sending, which ``run_evaluation`` already handles as an EOF.
    """
    if not max_memory_bytes:
        return
    try:
        import resource

        resource.setrlimit(resource.RLIMIT_AS, (max_memory_bytes, max_memory_bytes))
    except (ImportError, ValueError, OSError):  # pragma: no cover — platform-dependent
        # Not available (or not lowerable) everywhere; the timeout and the size
        # gates still apply, so this degrades rather than fails the request.
        pass


def _worker(conn: Any, kwargs: dict[str, Any]) -> None:
    _apply_memory_ceiling(kwargs.pop("max_memory_bytes", None))
    try:
        conn.send(("ok", evaluate_workbook(**kwargs)))
    except UnsupportedFunctionError as exc:
        conn.send(("error", "unsupported", exc.names))
    except EvaluationError as exc:
        conn.send(("error", "evaluation", str(exc)))
    except Exception as exc:  # noqa: BLE001 — an engine crash is still one request
        conn.send(("error", "internal", f"{type(exc).__name__}: {exc}"))
    finally:
        conn.close()


def _context() -> Any:
    """Prefer ``fork`` so the child inherits the already-imported engine.

    Importing ``formulas`` (which pulls scipy and numpy) costs seconds; forking
    reuses the parent's import. macOS has no usable ``fork`` for this, so local
    test runs fall back to ``spawn`` and simply pay the import.
    """
    try:
        return mp.get_context("fork")
    except ValueError:  # pragma: no cover — platform-dependent
        return mp.get_context("spawn")


def run_evaluation(timeout_seconds: float, **kwargs: Any) -> EvaluationResult:
    """Evaluate in a child process, or raise.

    ``max_memory_bytes`` (optional) caps the child's address space; the rest of
    ``kwargs`` is forwarded to ``evaluate_workbook``.

    Raises ``EvaluationTimeoutError``, ``UnsupportedFunctionError`` or
    ``EvaluationError`` — the same exceptions the caller would have seen from a
    direct call, reconstructed on this side of the pipe. A child killed by the
    memory ceiling surfaces as ``EvaluationError`` via the EOF path below.
    """
    ctx = _context()
    parent_conn, child_conn = ctx.Pipe(duplex=False)
    proc = ctx.Process(target=_worker, args=(child_conn, kwargs), daemon=True)
    proc.start()
    child_conn.close()  # only the child holds the write end; EOF now means "died"

    try:
        if not parent_conn.poll(timeout_seconds):
            raise EvaluationTimeoutError(
                f"evaluation exceeded {timeout_seconds:g}s and was cancelled"
            )
        try:
            message = parent_conn.recv()
        except EOFError as exc:
            raise EvaluationError("evaluation process died without a result") from exc
    finally:
        parent_conn.close()
        if proc.is_alive():
            proc.terminate()
        proc.join(timeout=5)

    if message[0] == "ok":
        return message[1]
    _, kind, payload = message
    if kind == "unsupported":
        raise UnsupportedFunctionError(payload)
    if kind == "evaluation":
        raise EvaluationError(payload)
    raise EvaluationError(payload)
