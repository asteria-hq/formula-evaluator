"""HTTP surface for the formula evaluator.

One endpoint over ``evaluate.py``, plus a health check:

  POST /evaluate   xlsx bytes (base64) + optional cell overrides → evaluated
                   values as JSON.

**Workbook bytes cross the boundary, not storage references.** This service
holds no storage credentials and no database handle, so the licence boundary
described in ``README.md`` § Licensing encloses as little as possible. The cost
is a base64 payload ~33% larger than the file; the size gate keeps that bounded.

Auth is a shared secret in ``X-API-Key``, compared in constant time.

The path operation is declared ``def`` (not ``async``): evaluation is blocking,
CPU-bound, subprocess-bound work, so FastAPI runs it in its threadpool without
stalling the event loop.
"""

from __future__ import annotations

import base64
import binascii
import hmac
import logging
import os
import time

from evaluate import EvaluationError, UnsupportedFunctionError
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field
from runner import EvaluationTimeoutError, run_evaluation

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
logger = logging.getLogger("formula_evaluator")

_API_KEY = os.environ.get("FORMULA_EVALUATOR_API_KEY", "")
# Explicit dev opt-out so an unset key never silently opens the service in prod.
_AUTH_DISABLED = os.environ.get("FORMULA_EVALUATOR_AUTH_DISABLED") == "1"


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        logger.warning("%s is not an integer — using default %d", name, default)
        return default


# Size and cost gates. Defaults are deliberately conservative: this endpoint runs
# user-supplied logic, and both limits are cheap to raise once real workbooks have
# been measured against them.
MAX_WORKBOOK_BYTES = _int_env("FORMULA_EVALUATOR_MAX_BYTES", 10 * 1024 * 1024)
MAX_FORMULA_CELLS = _int_env("FORMULA_EVALUATOR_MAX_FORMULA_CELLS", 20_000)
TIMEOUT_SECONDS = float(os.environ.get("FORMULA_EVALUATOR_TIMEOUT", "60"))
# `.xlsx` is a zip, so MAX_WORKBOOK_BYTES bounds the compressed upload only.
MAX_UNCOMPRESSED_BYTES = _int_env("FORMULA_EVALUATOR_MAX_UNCOMPRESSED_BYTES", 200 * 1024 * 1024)
# Address-space ceiling for the evaluating child. The cell cap bounds how many
# formulas there are, not how much each allocates: a few formulas over
# full-column ranges can exhaust memory long before the wall clock fires, and
# with one uvicorn worker that OOM is a whole-container outage, not one request.
MAX_MEMORY_BYTES = _int_env("FORMULA_EVALUATOR_MAX_MEMORY_MB", 2048) * 1024 * 1024

app = FastAPI(title="formula-evaluator", docs_url=None, redoc_url=None, openapi_url=None)


def _require_auth(x_api_key: str | None) -> None:
    if not _API_KEY:
        if _AUTH_DISABLED:
            logger.warning("FORMULA_EVALUATOR_API_KEY unset + AUTH_DISABLED=1 — auth bypassed")
            return
        logger.error("FORMULA_EVALUATOR_API_KEY not set and AUTH_DISABLED!=1 — denying")
        raise HTTPException(status_code=503, detail="service not configured")
    provided = x_api_key or ""
    if len(provided) != len(_API_KEY) or not hmac.compare_digest(provided, _API_KEY):
        raise HTTPException(status_code=401, detail="invalid api key")


class EvaluateRequest(BaseModel):
    workbook_b64: str = Field(..., description="Base64-encoded .xlsx bytes")
    overrides: dict[str, float | int | str | bool] = Field(
        default_factory=dict,
        description=(
            "What-if inputs as {'Sheet!A1': value}. An address that does not "
            "resolve to a cell in the workbook is a 422 — the engine would "
            "otherwise ignore it and return baseline numbers."
        ),
    )
    cells: list[str] | None = Field(
        None,
        description=(
            "Restrict the response to these 'Sheet!A1' addresses. Omit to return "
            "every formula cell. Overridden cells are always included."
        ),
    )
    allow_unsupported: bool = Field(
        False,
        description=(
            "Evaluate even when the workbook uses functions the engine lacks. Off "
            "by default: such a workbook can return plausible wrong numbers."
        ),
    )


class CellValueModel(BaseModel):
    value: float | int | str | bool | None = None
    error: str | None = Field(
        None, description="Excel error literal (#DIV/0!, #NAME?, …) when the cell errored"
    )


class EvaluateResponse(BaseModel):
    values: dict[str, CellValueModel]
    formula_cells: int = Field(..., description="Formula cells found in the workbook")
    overrides_applied: int
    sheets: list[str] = Field(..., description="Sheets that hold at least one formula")
    duration_ms: int


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/evaluate", response_model=EvaluateResponse)
def evaluate_endpoint(
    req: EvaluateRequest, x_api_key: str | None = Header(default=None)
) -> EvaluateResponse:
    _require_auth(x_api_key)

    try:
        data = base64.b64decode(req.workbook_b64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(
            status_code=400, detail=f"workbook_b64 is not valid base64: {exc}"
        ) from exc

    if not data:
        raise HTTPException(status_code=400, detail="workbook_b64 decoded to zero bytes")
    if len(data) > MAX_WORKBOOK_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"workbook is {len(data)} bytes, above the {MAX_WORKBOOK_BYTES} limit",
        )

    t0 = time.perf_counter()
    try:
        result = run_evaluation(
            TIMEOUT_SECONDS,
            data=data,
            overrides=req.overrides,
            cells=req.cells,
            max_formula_cells=MAX_FORMULA_CELLS,
            max_uncompressed_bytes=MAX_UNCOMPRESSED_BYTES,
            max_memory_bytes=MAX_MEMORY_BYTES,
            allow_unsupported=req.allow_unsupported,
        )
    except UnsupportedFunctionError as exc:
        # 422 with the names: the caller must be able to report *which* function
        # could not be handled rather than "evaluation failed".
        raise HTTPException(
            status_code=422,
            detail={
                "reason": "unsupported_functions",
                "functions": exc.names,
                "message": str(exc),
            },
        ) from exc
    except EvaluationTimeoutError as exc:
        raise HTTPException(status_code=504, detail=str(exc)) from exc
    except EvaluationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    duration_ms = int((time.perf_counter() - t0) * 1000)
    logger.info(
        "/evaluate %d bytes, %d formula cells, %d overrides, %d reported: %dms",
        len(data),
        result.formula_cells,
        result.overrides_applied,
        len(result.values),
        duration_ms,
    )
    return EvaluateResponse(
        values={
            ref: CellValueModel(value=cell.value, error=cell.error)
            for ref, cell in result.values.items()
        },
        formula_cells=result.formula_cells,
        overrides_applied=result.overrides_applied,
        sheets=result.sheets,
        duration_ms=duration_ms,
    )
