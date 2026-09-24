"""Evaluation behaviour.

The tests that matter most here are not "does SUM add up" — they are the three
engine behaviours that can produce a *confidently wrong* answer:

- an override key the engine silently ignores (``test_unknown_override_*``),
- a function the engine lacks but computes anyway (``test_unsupported_*``),
- a cell with no node in the model, which must not read as zero.
"""

from typing import Any

import pytest
from conftest import WorkbookFactory
from evaluate import (
    EvaluationError,
    EvaluationResult,
    UnsupportedFunctionError,
    check_archive_size,
    collect_formula_cells,
    evaluate_workbook,
    parse_cell_ref,
    unsupported_functions,
)

CELL_CAP = 10_000


def evaluate(data: bytes, **kwargs: Any) -> EvaluationResult:
    kwargs.setdefault("max_formula_cells", CELL_CAP)
    return evaluate_workbook(data, **kwargs)


class TestFormulaCollection:
    def test_collects_formulas_per_sheet(self, simple_workbook: bytes) -> None:
        cells = collect_formula_cells(simple_workbook)
        assert cells["Data"]["E1"] == "=SUM(C2:C4)"
        assert set(cells["Data"]) == {"E1", "E2", "E3", "E4"}

    def test_sheets_without_formulas_are_absent(self, workbook_factory: WorkbookFactory) -> None:
        assert collect_formula_cells(workbook_factory()) == {}

    def test_unreadable_workbook_is_an_evaluation_error(self) -> None:
        with pytest.raises(EvaluationError, match="cannot read workbook"):
            collect_formula_cells(b"not a workbook")


class TestUnsupportedDetection:
    """Static detection, because behaviour is not a reliable signal.

    ``INDIRECT`` and ``LET`` are absent from the engine registry yet return the
    *correct* answer for simple arguments — so a results-based check would pass
    them through. Both are listed here to pin that they are refused anyway.
    """

    @pytest.mark.parametrize(
        "formula, expected",
        [
            ("=SUM(C2:C4)", []),
            ('=SUMIFS(C2:C4,A2:A4,"EMEA")', []),
            ("=IFERROR(SUM(C2:C4),0)", []),
            ("=SUM(OFFSET(C2,0,0,3,1))", ["OFFSET"]),
            ('=SUM(INDIRECT("C2:C4"))', ["INDIRECT"]),
            ("=LET(x,C2,x*2)", ["LET"]),
            ("=SUM(SEQUENCE(3))", ["SEQUENCE"]),
            ("=_xlfn.XLOOKUP(1,A2:A4,C2:C4)", []),
        ],
    )
    def test_registry_membership(self, formula: str, expected: object) -> None:
        assert unsupported_functions({"Data": {"G1": formula}}) == expected

    def test_a_string_literal_is_not_a_function_call(self) -> None:
        """`=CONCATENATE("SEQUENCE(1)")` is text, not a call to SEQUENCE."""
        assert unsupported_functions({"Data": {"G1": '=CONCATENATE("SEQUENCE(1)")'}}) == []

    def test_a_defined_name_is_scanned_too(self) -> None:
        """A named range can hide an unimplemented function from a cell-only walk.

        That path would otherwise skip the gate entirely and evaluate anyway —
        the "plausible wrong number" outcome the gate exists to prevent.
        """
        found = unsupported_functions({}, {"MyRange": "SUM(OFFSET(Data!$A$1,0,0,3,1))"})
        assert found == ["OFFSET"]

    def test_names_are_deduplicated_and_sorted(self) -> None:
        found = unsupported_functions(
            {"Data": {"G1": "=OFFSET(C2,1,1)", "G2": "=OFFSET(C3,1,1)+SEQUENCE(2)"}}
        )
        assert found == ["OFFSET", "SEQUENCE"]

    def test_evaluation_refuses_and_names_the_function(
        self, workbook_factory: WorkbookFactory
    ) -> None:
        data = workbook_factory({"Data": {"G1": "=SUM(OFFSET(C2,0,0,3,1))"}})
        with pytest.raises(UnsupportedFunctionError, match="OFFSET") as exc:
            evaluate(data)
        assert exc.value.names == ["OFFSET"]

    def test_allow_unsupported_opts_into_best_effort(
        self, workbook_factory: WorkbookFactory
    ) -> None:
        """The escape hatch exists, and INDIRECT is why it is not the default."""
        data = workbook_factory({"Data": {"G1": '=SUM(INDIRECT("C2:C4"))'}})
        result = evaluate(data, allow_unsupported=True)
        assert result.values["Data!G1"].value == pytest.approx(450.0)


class TestEvaluation:
    def test_uncached_formulas_get_real_values(self, simple_workbook: bytes) -> None:
        """The core case: every one of these reads back as 0 without an evaluator."""
        result = evaluate(simple_workbook)
        assert result.values["Data!E1"].value == pytest.approx(450.0)
        assert result.values["Data!E2"].value == pytest.approx(250.0)
        assert result.values["Data!E3"].value == pytest.approx(900.0)

    def test_excel_errors_are_carried_as_errors_not_values(self, simple_workbook: bytes) -> None:
        cell = evaluate(simple_workbook).values["Data!E4"]
        assert cell.value is None
        assert cell.error == "#DIV/0!"

    def test_reports_every_formula_cell_by_default(self, simple_workbook: bytes) -> None:
        result = evaluate(simple_workbook)
        assert set(result.values) == {"Data!E1", "Data!E2", "Data!E3", "Data!E4"}
        assert result.formula_cells == 4
        assert result.sheets == ["Data"]

    def test_cells_argument_restricts_the_response(self, simple_workbook: bytes) -> None:
        result = evaluate(simple_workbook, cells=["Data!E1"])
        assert set(result.values) == {"Data!E1"}

    def test_cell_with_no_node_reports_na_rather_than_zero(self, simple_workbook: bytes) -> None:
        """Absence must never be readable as zero."""
        cell = evaluate(simple_workbook, cells=["Data!Z99"]).values["Data!Z99"]
        assert cell.value is None
        assert cell.error == "#N/A"

    def test_formula_cell_cap_is_enforced(self, simple_workbook: bytes) -> None:
        with pytest.raises(EvaluationError, match="above the 2 limit"):
            evaluate_workbook(simple_workbook, max_formula_cells=2)


class TestArchiveSizeGate:
    """`.xlsx` is a zip, so the upload size bounds compressed bytes only.

    The formula-cell cap cannot stand in for this: it needs the file parsed
    first, which is the very step a decompression bomb attacks.
    """

    def test_inflation_above_the_limit_is_rejected(self, simple_workbook: bytes) -> None:
        with pytest.raises(EvaluationError, match="uncompressed limit"):
            check_archive_size(simple_workbook, max_uncompressed_bytes=64)

    def test_a_normal_workbook_passes(self, simple_workbook: bytes) -> None:
        check_archive_size(simple_workbook, max_uncompressed_bytes=100 * 1024 * 1024)

    def test_a_non_zip_upload_is_an_evaluation_error(self) -> None:
        with pytest.raises(EvaluationError, match="cannot read workbook"):
            check_archive_size(b"not a zip at all", max_uncompressed_bytes=1024)

    def test_the_gate_runs_before_parsing(self, simple_workbook: bytes) -> None:
        with pytest.raises(EvaluationError, match="uncompressed limit"):
            evaluate(simple_workbook, max_uncompressed_bytes=64)


class TestWhatIf:
    def test_override_propagates_through_dependents(self, simple_workbook: bytes) -> None:
        result = evaluate(simple_workbook, overrides={"Data!C2": 500})
        assert result.values["Data!E1"].value == pytest.approx(850.0)
        assert result.values["Data!E3"].value == pytest.approx(1700.0)
        assert result.overrides_applied == 1

    def test_overridden_cell_is_always_reported(self, simple_workbook: bytes) -> None:
        result = evaluate(simple_workbook, overrides={"Data!C2": 500}, cells=["Data!E1"])
        assert set(result.values) == {"Data!E1", "Data!C2"}

    def test_a_formula_cell_can_itself_be_overridden(self, simple_workbook: bytes) -> None:
        result = evaluate(simple_workbook, overrides={"Data!E1": 1000})
        assert result.values["Data!E3"].value == pytest.approx(2000.0)

    def test_unknown_override_is_rejected(self, simple_workbook: bytes) -> None:
        """Without this the engine returns baseline numbers and raises nothing."""
        with pytest.raises(EvaluationError, match="does not resolve to a cell"):
            evaluate(simple_workbook, overrides={"Data!ZZ1": 1})

    def test_unknown_sheet_in_override_is_rejected(self, simple_workbook: bytes) -> None:
        with pytest.raises(EvaluationError, match="does not resolve to a cell"):
            evaluate(simple_workbook, overrides={"NoSuchSheet!A1": 1})

    def test_sheet_name_case_does_not_decide_the_answer(self, simple_workbook: bytes) -> None:
        """The engine uppercases sheet names in its keys; we must not leak that.

        Passing the engine a mis-cased key makes it ignore the override silently,
        so this asserts the override actually *bit* rather than merely that the
        call succeeded.
        """
        result = evaluate(simple_workbook, overrides={"data!C2": 500})
        assert result.values["Data!E1"].value == pytest.approx(850.0)


class TestParseCellRef:
    @pytest.mark.parametrize(
        "address, expected",
        [
            ("Data!A1", ("Data", "A1")),
            ("'Raw Data'!B2", ("Raw Data", "B2")),
            ("Summary!AA100", ("Summary", "AA100")),
        ],
    )
    def test_valid_addresses(self, address: str, expected: object) -> None:
        assert parse_cell_ref(address) == expected

    @pytest.mark.parametrize("address", ["A1", "", "Data!", "!A1"])
    def test_invalid_addresses(self, address: str) -> None:
        with pytest.raises(EvaluationError, match="must be 'Sheet!Ref'"):
            parse_cell_ref(address)
