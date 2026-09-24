# Formula evaluator

Evaluates the formulas in a submitted `.xlsx` and recomputes them under changed inputs.

Two capabilities on one engine:

1. **Evaluate.** A workbook arrives with formulas but no cached values, so an ordinary reader reports `0` for those cells. This computes the real answer.
2. **What-if.** Override cells and recompute.

## Contract

`POST /evaluate`, auth via `X-API-Key` (shared secret, compared in constant time).

```jsonc
{
  "workbook_b64": "UEsDBBQ…",              // base64 .xlsx bytes
  "overrides": {"Summary!B1": 500},        // optional what-if inputs
  "cells": ["Summary!B2", "Summary!B3"],   // optional; omit for every formula cell
  "allow_unsupported": false
}
```

```jsonc
{
  "values": {
    "Summary!B1": {"value": 500,  "error": null},
    "Summary!B2": {"value": 1000, "error": null},
    "Summary!B3": {"value": null, "error": "#DIV/0!"}
  },
  "formula_cells": 12,
  "overrides_applied": 1,
  "sheets": ["Summary"],
  "duration_ms": 412
}
```

**Bytes cross the boundary, not storage references** — deliberate: this service gets no storage credentials, so the licence boundary encloses as little as possible. The cost is a payload ~33% larger than the file; the size gate bounds it.

`value` and `error` are separate fields so a caller cannot mistake the string `"#DIV/0!"` for a result.

### Status codes

| Code | Meaning |
|---|---|
| 400 | `workbook_b64` is not valid base64, or decoded to nothing |
| 401 | bad `X-API-Key` |
| 413 | workbook above `FORMULA_EVALUATOR_MAX_BYTES` |
| 422 | unreadable workbook, breached decompressed-size or formula-cell gate, an override that does not resolve, or unsupported functions (detail carries `functions`) |
| 503 | no API key configured and `AUTH_DISABLED != 1` |
| 504 | evaluation exceeded `FORMULA_EVALUATOR_TIMEOUT`; the child was killed |

## Three engine behaviours the contract is built around

All probed against `formulas` 1.3.4 rather than taken from its docs.

**A mis-cased override key is silently ignored.** The engine's cell keys are `'[workbook.xlsx]SHEETNAME'!A1` with the sheet name uppercased. Passing `'[workbook.xlsx]Summary'!B1` to `calculate(inputs=…)` returns the *baseline* numbers and raises nothing — a what-if that quietly answers a different question. Every override is therefore resolved against the loaded model before it is applied, and an unknown address is a 422.

**Unsupported functions fail inconsistently, so detection is static.** Of the five the engine lacks:

| Function | In registry | Actual behaviour |
|---|---|---|
| `OFFSET` | no | `#NAME?` — loud |
| `SEQUENCE` | no | `#NAME?` — loud |
| `LAMBDA` | no | `#VALUE!` — loud |
| `INDIRECT` | no | **correct answer** with a literal argument; `#NAME?` with a dynamic one |
| `LET` | no | **correct answer** |

Because two of them return plausible numbers, "did it produce a value" is not a soundness test. Every function name is checked against the engine's 645-entry registry *before* evaluation, and an unimplemented one is refused by name. `allow_unsupported: true` exists for callers that knowingly want best-effort.

The scan covers **defined names as well as cell formulas**: a named range whose definition uses an unimplemented function is invisible to a cell-only walk, so that workbook would skip the gate and be evaluated anyway. String literals are stripped first, so `=CONCATENATE("SEQUENCE(1)")` is not mistaken for a call.

**Errors are values.** `#DIV/0!`, `#REF!`, `#NAME?` come back as `XlError` objects and are serialised into the `error` field. A requested cell the model has no node for reports `#N/A` rather than being omitted, so absence is never read as zero.

## Cost and trust gates

Evaluating a submitted workbook is executing caller-supplied logic, and the engine builds a dependency graph over the whole workbook.

| Gate | Env var | Default |
|---|---|---|
| Raw workbook size (compressed) | `FORMULA_EVALUATOR_MAX_BYTES` | 10 MiB |
| Decompressed size | `FORMULA_EVALUATOR_MAX_UNCOMPRESSED_BYTES` | 200 MiB |
| Formula cells | `FORMULA_EVALUATOR_MAX_FORMULA_CELLS` | 20 000 |
| Child address space | `FORMULA_EVALUATOR_MAX_MEMORY_MB` | 2048 |
| Wall clock | `FORMULA_EVALUATOR_TIMEOUT` | 60 s |

The first three are pre-flight — a zip directory read, then a cheap `openpyxl` read — so a pathological workbook is rejected before the expensive graph build.

## Environment

| Var | Purpose |
|---|---|
| `FORMULA_EVALUATOR_API_KEY` | Shared secret (set as a **secret** env var) |
| `FORMULA_EVALUATOR_AUTH_DISABLED` | `1` to bypass auth locally; without it an unset key returns 503 rather than opening the service |
| `FORMULA_EVALUATOR_MAX_BYTES`, `…_MAX_UNCOMPRESSED_BYTES`, `…_MAX_FORMULA_CELLS`, `…_MAX_MEMORY_MB`, `…_TIMEOUT` | Gates above |
| `LOG_LEVEL` | Default `INFO` |

## Build, test, run

```bash
python3.11 -m venv .venv && .venv/bin/pip install -r requirements.txt -r requirements-dev.txt
.venv/bin/python -m pytest tests -v

docker build --platform linux/amd64 -t formula-evaluator .
docker run --rm -p 8080:8080 -e FORMULA_EVALUATOR_AUTH_DISABLED=1 formula-evaluator
```
