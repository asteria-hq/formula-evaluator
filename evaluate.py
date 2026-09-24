from __future__ import annotations

import logging
import re
import zipfile
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path

# The engine ships no type stubs, and by design it is installed only in this
# container's image — never anywhere the repo-wide mypy run can see it.
import formulas  # type: ignore[import-not-found]
from formulas.functions import get_functions  # type: ignore[import-not-found]
from formulas.tokens.operand import XlError  # type: ignore[import-not-found]
from openpyxl import load_workbook

logger = logging.getLogger("formula_evaluator")

# The engine embeds the file's name in every cell key, so we always load from this
# exact name and the key mapping stays deterministic regardless of what the user
# called their upload.
WORKBOOK_FILENAME = "workbook.xlsx"

# A function call in a formula: a name followed by an open paren. Cell references
# and table structured references (`Table1[Col]`) are not followed by `(`, so they
# do not match. Names are compared uppercased against the engine registry.
_FUNC_CALL_RE = re.compile(r"\b([A-Z][A-Z0-9_.]*)\s*\(", re.IGNORECASE)

# Excel string literals are double-quoted, with an embedded quote written "". They
# are stripped before the scan above runs: otherwise `=CONCATENATE("SEQUENCE(1)")`
# trips the unsupported-functions gate on text that is data, not a call.
_STRING_LITERAL_RE = re.compile(r'"(?:[^"]|"")*"')

# Excel lets a formula qualify a post-2007 function as `_xlfn.NAME`; the registry
# holds both spellings, so strip the prefix before the membership test.
_XLFN_PREFIX = "_XLFN."

# Ceiling on the workbook's *decompressed* size. Bounding the upload bounds only
# the compressed bytes; `.xlsx` is a zip. Overridden per request by the service.
DEFAULT_MAX_UNCOMPRESSED_BYTES = 200 * 1024 * 1024


class EvaluationError(Exception):
    """Raised for any caller-correctable failure. The message reaches the caller."""


class UnsupportedFunctionError(EvaluationError):
    """The workbook uses a function the engine does not implement.

    Deliberately fatal rather than best-effort: the engine may return a plausible
    number for an unimplemented function (``INDIRECT`` with a literal argument
    does exactly that), and a wrong number presented as an answer is worse than a
    refusal that names the function.
    """

    def __init__(self, names: list[str]) -> None:
        self.names = names
        joined = ", ".join(names)
        super().__init__(
            f"workbook uses {len(names)} function(s) this evaluator does not implement: {joined}"
        )


@dataclass(frozen=True)
class CellValue:
    """One evaluated cell.

    ``error`` holds the Excel error literal (``#DIV/0!``, ``#NAME?``, ``#REF!``…)
    when the cell evaluated to one, and ``value`` is then ``None``. The two are
    kept apart so a caller cannot mistake the string ``"#DIV/0!"`` for a result.
    """

    value: float | int | str | bool | None
    error: str | None = None


@dataclass
class EvaluationResult:
    values: dict[str, CellValue] = field(default_factory=dict)
    formula_cells: int = 0
    overrides_applied: int = 0
    sheets: list[str] = field(default_factory=list)


def _formula_text(value: object) -> str | None:
    """Return the formula a cell holds, or ``None`` for a plain value.

    A cell carries its formula as a bare ``str`` or, for legacy CSE and modern
    dynamic-array formulas, as an ``ArrayFormula`` whose ``.text`` holds it —
    matching only ``str`` drops every array formula silently.
    """
    if isinstance(value, str):
        return value if value.startswith("=") else None
    text = getattr(value, "text", None)
    if isinstance(text, str) and text.startswith("="):
        return text
    return None


def check_archive_size(data: bytes, max_uncompressed_bytes: int) -> None:
    """Reject a workbook that inflates past ``max_uncompressed_bytes``.

    ``.xlsx`` is a zip container, so bounding the *upload* bounds the compressed
    size only. A small, highly compressible crafted file can inflate enormously
    inside ``load_workbook`` — and the formula-cell gate cannot catch it, because
    that gate needs the file parsed first. The zip central directory declares the
    uncompressed size of every member, so this costs a directory read.
    """
    try:
        with zipfile.ZipFile(BytesIO(data)) as archive:
            total = sum(info.file_size for info in archive.infolist())
    except zipfile.BadZipFile as exc:
        raise EvaluationError(f"cannot read workbook: {exc}") from exc
    if total > max_uncompressed_bytes:
        raise EvaluationError(
            f"workbook inflates to {total} bytes, above the "
            f"{max_uncompressed_bytes} uncompressed limit"
        )


def collect_formulas(data: bytes) -> tuple[dict[str, dict[str, str]], dict[str, str]]:
    """Read every formula in the workbook: ``(cells, defined_names)``.

    ``cells`` maps ``{sheet_name: {ref: formula}}``; ``defined_names`` maps a
    name to the formula it resolves to.

    **Defined names are scanned too, and that is not cosmetic.** A named range
    whose definition uses an unimplemented function is invisible to a cell-only
    walk, so the workbook would sail past the unsupported-function gate and be
    evaluated anyway — the exact "plausible wrong number" outcome that gate
    exists to prevent. Both workbook-global and worksheet-local names are read.

    Uses ``openpyxl`` (already an engine dependency) rather than the engine's own
    model so the gates and the scan can run *before* the expensive graph build.
    """
    cells: dict[str, dict[str, str]] = {}
    names: dict[str, str] = {}
    try:
        wb = load_workbook(BytesIO(data), data_only=False, read_only=True)
    except Exception as exc:  # noqa: BLE001 — any malformed upload is a 422
        raise EvaluationError(f"cannot read workbook: {exc}") from exc
    try:
        for sheet in wb.worksheets:
            found = {
                cell.coordinate: text
                for row in sheet.iter_rows()
                for cell in row
                if (text := _formula_text(getattr(cell, "value", None))) is not None
            }
            if found:
                cells[sheet.title] = found
        names.update(_defined_names(wb))
        for sheet in wb.worksheets:
            names.update(_defined_names(sheet, prefix=f"{sheet.title}!"))
    finally:
        wb.close()
    return cells, names


def _defined_names(holder: object, prefix: str = "") -> dict[str, str]:
    """Defined names on a workbook or worksheet, as ``{name: definition}``.

    Tolerant by construction: a workbook with no names, or an openpyxl version
    that shapes the collection differently, must degrade to "no names" rather
    than fail the whole evaluation.
    """
    container = getattr(holder, "defined_names", None)
    if not container:
        return {}
    found: dict[str, str] = {}
    try:
        for name in container:
            definition = getattr(container[name], "value", None)
            if isinstance(definition, str) and definition:
                found[f"{prefix}{name}"] = definition
    except (AttributeError, KeyError, TypeError):  # pragma: no cover — shape drift
        logger.warning("could not read defined names from %s", type(holder).__name__)
    return found


def collect_formula_cells(data: bytes) -> dict[str, dict[str, str]]:
    """``collect_formulas``' cell half. Kept for callers that only want cells."""
    return collect_formulas(data)[0]


def unsupported_functions(
    formula_cells: dict[str, dict[str, str]],
    defined_names: dict[str, str] | None = None,
) -> list[str]:
    """Names used by the workbook that the engine does not implement, sorted.

    Static and exhaustive over both cell formulas and defined names: every
    function name is checked against the engine's 645-entry registry. See the
    module docstring for why the alternative — inspecting results for ``#NAME?``
    — is unsound.
    """
    registry = get_functions()
    seen: set[str] = set()
    formulas_to_scan = [f for sheet in formula_cells.values() for f in sheet.values()]
    formulas_to_scan.extend((defined_names or {}).values())
    for formula in formulas_to_scan:
        for raw in _FUNC_CALL_RE.findall(_STRING_LITERAL_RE.sub('""', formula)):
            name = raw.upper()
            if name.startswith(_XLFN_PREFIX):
                name = name[len(_XLFN_PREFIX) :]
            if name in registry or f"{_XLFN_PREFIX}{name}" in registry:
                continue
            seen.add(name)
    return sorted(seen)


def _engine_key(sheet: str, ref: str) -> str:
    """Our ``Sheet!A1`` → the engine's ``'[workbook.xlsx]SHEET'!A1``."""
    return f"'[{WORKBOOK_FILENAME}]{sheet.upper()}'!{ref.upper()}"


def parse_cell_ref(address: str) -> tuple[str, str]:
    """Split a wire address ``Sheet!A1`` into ``(sheet, ref)``.

    Sheet names containing ``!`` are not addressable and are rejected rather than
    guessed at; a quoted ``'Raw Data'!A1`` is accepted and unquoted.
    """
    if "!" not in address:
        raise EvaluationError(f"cell address must be 'Sheet!Ref' (got {address!r})")
    sheet, _, ref = address.rpartition("!")
    sheet = sheet.strip()
    if sheet.startswith("'") and sheet.endswith("'") and len(sheet) >= 2:
        sheet = sheet[1:-1]
    if not sheet or not ref:
        raise EvaluationError(f"cell address must be 'Sheet!Ref' (got {address!r})")
    return sheet, ref.strip()


def _to_python(raw: object) -> CellValue:
    """Normalise one engine value into JSON-safe ``CellValue``."""
    if isinstance(raw, XlError):
        return CellValue(value=None, error=str(raw))
    item = getattr(raw, "item", None)  # numpy scalar → python scalar
    if callable(item):
        try:
            raw = item()
        except (ValueError, AttributeError):
            pass
    if raw is None or isinstance(raw, bool | int | str):
        return CellValue(value=raw)
    if isinstance(raw, float):
        # JSON has no NaN/Infinity; surface them as errors rather than emitting
        # literals that json.loads accepts but strict parsers reject.
        if raw != raw or raw in (float("inf"), float("-inf")):
            return CellValue(value=None, error="#NUM!")
        return CellValue(value=raw)
    return CellValue(value=str(raw))


def _cell_from_solution(solution: dict, key: str) -> CellValue | None:
    """Read one cell out of a solved model, or ``None`` if the key is absent."""
    if key not in solution:
        return None
    holder = solution[key]
    value = getattr(holder, "value", holder)
    try:
        raw = value[0, 0]
    except (TypeError, IndexError, KeyError):
        raw = value
    return _to_python(raw)


def evaluate_workbook(
    data: bytes,
    overrides: dict[str, float | int | str | bool] | None = None,
    cells: list[str] | None = None,
    *,
    max_formula_cells: int,
    max_uncompressed_bytes: int = DEFAULT_MAX_UNCOMPRESSED_BYTES,
    allow_unsupported: bool = False,
) -> EvaluationResult:
    """Evaluate ``data``, optionally under ``overrides``, and return cell values.

    ``cells`` restricts the response to specific ``Sheet!A1`` addresses; omitted,
    every formula cell in the workbook is returned. Overridden cells are always
    included — a what-if answer that omits the input it changed is hard to read.

    Raises ``UnsupportedFunctionError`` before doing any work when the workbook
    uses a function the engine lacks (unless ``allow_unsupported``), and
    ``EvaluationError`` for an unreadable workbook, a breached size gate, or an
    override naming a cell the model does not contain.
    """
    overrides = overrides or {}
    # Cheapest gate first: a zip directory read, before anything is decompressed.
    check_archive_size(data, max_uncompressed_bytes)
    formula_cells, defined_names = collect_formulas(data)
    total = sum(len(v) for v in formula_cells.values())

    if total > max_formula_cells:
        raise EvaluationError(
            f"workbook has {total} formula cells, above the {max_formula_cells} limit"
        )

    missing = unsupported_functions(formula_cells, defined_names)
    if missing and not allow_unsupported:
        raise UnsupportedFunctionError(missing)

    import tempfile

    with tempfile.TemporaryDirectory(prefix="formula-eval-") as tmp:
        path = Path(tmp) / WORKBOOK_FILENAME
        path.write_bytes(data)
        try:
            model = formulas.ExcelModel().loads(str(path)).finish()
        except Exception as exc:  # noqa: BLE001
            raise EvaluationError(f"cannot build workbook model: {exc}") from exc

        # Resolve overrides against the *baseline* solution before applying them.
        # The engine ignores an unknown or mis-cased input key without complaint
        # and hands back baseline numbers, so this is the only thing standing
        # between a typo and a confidently wrong what-if answer.
        try:
            baseline = model.calculate()
        except Exception as exc:  # noqa: BLE001
            raise EvaluationError(f"evaluation failed: {exc}") from exc

        inputs: dict[str, object] = {}
        for address, value in overrides.items():
            sheet, ref = parse_cell_ref(address)
            key = _engine_key(sheet, ref)
            if key not in baseline:
                raise EvaluationError(
                    f"override {address!r} does not resolve to a cell in this workbook"
                )
            inputs[key] = value

        solution = baseline
        if inputs:
            try:
                solution = model.calculate(inputs=inputs)
            except Exception as exc:  # noqa: BLE001
                raise EvaluationError(f"what-if evaluation failed: {exc}") from exc

    # Which addresses to report.
    if cells is not None:
        wanted = [parse_cell_ref(a) for a in cells]
    else:
        wanted = [(sheet, ref) for sheet, refs in formula_cells.items() for ref in refs]
    for address in overrides:
        pair = parse_cell_ref(address)
        if pair not in wanted:
            wanted.append(pair)

    result = EvaluationResult(
        formula_cells=total,
        overrides_applied=len(inputs),
        sheets=sorted(formula_cells),
    )
    for sheet, ref in wanted:
        cell = _cell_from_solution(solution, _engine_key(sheet, ref))
        if cell is None:
            # Requested a cell the model has no node for (an empty cell, or a
            # sheet that holds no formulas). Reported as an explicit error rather
            # than omitted, so the caller never reads absence as zero.
            result.values[f"{sheet}!{ref}"] = CellValue(value=None, error="#N/A")
            continue
        result.values[f"{sheet}!{ref}"] = cell
    return result
