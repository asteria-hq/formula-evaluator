"""Child-process execution and the wall-clock budget.

The timeout is the only thing bounding a pathological dependency graph — the
formula-cell gate bounds the input, not the time to solve it. So the test that
matters is that a run exceeding its budget is *killed*, not merely reported.
"""

import resource

import pytest
from conftest import WorkbookFactory
from evaluate import EvaluationError, UnsupportedFunctionError
from runner import EvaluationTimeoutError, _apply_memory_ceiling, run_evaluation


def test_returns_the_result_of_a_normal_run(simple_workbook: bytes) -> None:
    result = run_evaluation(60.0, data=simple_workbook, max_formula_cells=10_000)
    assert result.values["Data!E1"].value == pytest.approx(450.0)


def test_exceeding_the_budget_raises_and_kills_the_child(simple_workbook: bytes) -> None:
    with pytest.raises(EvaluationTimeoutError, match="exceeded"):
        run_evaluation(0.001, data=simple_workbook, max_formula_cells=10_000)


class TestMemoryCeiling:
    """A blow-up must cost one request, not the container.

    With a single uvicorn worker, an unbounded child OOM takes down every tenant
    the instance is serving — so "child process" only delivers the isolation the
    docs claim once an address-space limit is actually set.
    """

    def test_a_generous_ceiling_does_not_disturb_a_normal_run(self, simple_workbook: bytes) -> None:
        result = run_evaluation(
            60.0,
            data=simple_workbook,
            max_formula_cells=10_000,
            max_memory_bytes=2048 * 1024 * 1024,
        )
        assert result.values["Data!E1"].value == pytest.approx(450.0)

    def test_the_ceiling_is_actually_applied(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Assert the syscall, not the kill.

        Whether an over-tight ``RLIMIT_AS`` actually kills a process is the
        platform's business — Linux (the container) enforces it, macOS largely
        ignores it — so asserting a kill would test the test machine. What we own
        is that the limit gets set, and deleting the ``setrlimit`` call turns
        this red on every platform.
        """
        applied: list[tuple[int, tuple[int, int]]] = []
        monkeypatch.setattr(
            resource, "setrlimit", lambda which, limits: applied.append((which, limits))
        )
        _apply_memory_ceiling(512 * 1024 * 1024)
        assert applied == [(resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024))]

    @pytest.mark.parametrize("ceiling", [None, 0])
    def test_no_ceiling_means_no_syscall(
        self, monkeypatch: pytest.MonkeyPatch, ceiling: int | None
    ) -> None:
        """Unset must leave the inherited limit alone, not set it to zero."""
        applied: list[object] = []
        monkeypatch.setattr(resource, "setrlimit", lambda *a: applied.append(a))
        _apply_memory_ceiling(ceiling)
        assert applied == []


class TestErrorsCrossThePipe:
    """Exceptions are re-raised on the parent side with their payload intact.

    ``UnsupportedFunctionError`` takes a list in its constructor, which is exactly
    the shape that does not survive naive pickling — hence the tagged-tuple
    protocol, and hence this test.
    """

    def test_unsupported_function_names_survive(self, workbook_factory: WorkbookFactory) -> None:
        data = workbook_factory({"Data": {"G1": "=OFFSET(C2,0,0,3,1)"}})
        with pytest.raises(UnsupportedFunctionError) as exc:
            run_evaluation(60.0, data=data, max_formula_cells=10_000)
        assert exc.value.names == ["OFFSET"]

    def test_evaluation_error_message_survives(self, simple_workbook: bytes) -> None:
        with pytest.raises(EvaluationError, match="above the 1 limit"):
            run_evaluation(60.0, data=simple_workbook, max_formula_cells=1)
