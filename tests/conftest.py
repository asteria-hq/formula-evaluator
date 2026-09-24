"""Make the service modules importable, and build workbook fixtures.

Tests here import ``app``, ``evaluate`` and ``runner``, which are flat at the
source dir (this file's parent's parent).

The workbook factory writes formulas **without** a cached value on purpose. That
is the case this service exists for: a file authored elsewhere, whose formulas an
ordinary reader resolves to ``0`` because no cached value was ever stored. A
fixture that stamped cached values would pass whether or not the engine ran.
"""

import sys
from collections.abc import Callable
from pathlib import Path

import pytest
import xlsxwriter

# A call to the factory below: formulas dict (+ optional rows) -> .xlsx bytes.
WorkbookFactory = Callable[..., bytes]

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


@pytest.fixture
def workbook_factory(tmp_path: Path) -> WorkbookFactory:
    """Build an .xlsx and return its bytes.

    ``formulas`` maps each sheet/cell to ``{ref: formula}``; data rows are written
    to the ``Data`` sheet so the default fixture has something to compute over.
    """

    def build(
        formulas: dict[str, dict[str, str]] | None = None,
        *,
        rows: tuple[tuple[str, int], ...] = (("EMEA", 100), ("EMEA", 150), ("APAC", 200)),
    ) -> bytes:
        path = tmp_path / "fixture.xlsx"
        wb = xlsxwriter.Workbook(str(path))
        data = wb.add_worksheet("Data")
        data.write_string(0, 0, "region")
        data.write_string(0, 2, "amount")
        for i, (region, amount) in enumerate(rows, start=1):
            data.write_string(i, 0, region)
            data.write_number(i, 2, amount)

        for sheet_name, cells in (formulas or {}).items():
            sheet = data if sheet_name == "Data" else wb.add_worksheet(sheet_name)
            for ref, formula in cells.items():
                # No cached value — see the module docstring.
                sheet.write_formula(ref, formula)
        wb.close()
        return path.read_bytes()

    return build


@pytest.fixture
def simple_workbook(workbook_factory: WorkbookFactory) -> bytes:
    """A workbook whose four formulas span the cases that matter.

    ``E4`` divides by zero deliberately: an Excel error is a *value* the contract
    has to carry, not an exception.
    """
    return workbook_factory(
        {
            "Data": {
                "E1": "=SUM(C2:C4)",
                "E2": '=SUMIFS(C2:C4,A2:A4,"EMEA")',
                "E3": "=E1*2",
                "E4": "=C2/0",
            }
        }
    )
