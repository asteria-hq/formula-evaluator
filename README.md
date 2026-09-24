# Formula evaluator

Evaluates the formulas in a submitted `.xlsx` and recomputes them under changed inputs.

Two capabilities on one engine:

1. **Evaluate.** A workbook arrives with formulas but no cached values, so an ordinary reader reports `0` for those cells. This computes the real answer.
2. **What-if.** Override cells and recompute.

## Licensing — why this is its own service

`formulas` is **EUPL 1.1+**. Both 1.1 and 1.2 define Distribution/Communication to include *"providing access to its essential functionalities"* — an explicit ASP/SaaS clause. Serving spreadsheet evaluation over a network triggers Article 5. Unlike GPLv3, pure SaaS is **not** a way out; EUPL is closer to AGPL in effect, and the `+` in "1.1 or later" offers no escape because the clause is in both versions.

EUPL Article 1 leaves *what counts as a Derivative Work* to national copyright law rather than settling it. That question is unresolvable without counsel, so this design makes the answer not matter:

- The engine is imported by **`evaluate.py` and nothing else**, in an image that holds no storage credentials and no database handle.
- Worst case — the wrapper is a Derivative Work — costs the source of a few hundred lines of glue, which `NOTICE` already offers under EUPL 1.2.
- Anything else is out of scope under any reading, because it reaches this service only over HTTP.

This bounds the blast radius; it does **not** remove the Article 5 trigger. What Article 5 then requires is the Work (unmodified, already public — `NOTICE` indicates the upstream repository) plus any Derivative Work.

**Hard rule:** `formulas` must never be installed alongside a caller. A single convenience `import` would quietly undo the whole boundary.

### Why a container, not a serverless function

`formulas` pulls `scipy`, `numpy` and `schedula`, which is large against a typical function package limit. Native deps also have to match the runtime's libc — wheels built against glibc look fine in a zip and fail to `dlopen` on a musl runtime. An image controls its own base and avoids the class of problem entirely.

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

**The decompressed gate is not redundant with the upload gate.** `.xlsx` is a zip, so bounding the upload bounds compressed bytes only; a small, highly compressible crafted file can inflate enormously inside `load_workbook`, and the formula-cell cap cannot catch it because that cap needs the file parsed first.

**The cell cap bounds how many formulas there are, not how much each allocates.** A handful of formulas over full-column ranges (`=SUM(A1:A1048576)`, `SUMPRODUCT` over the same) materialise million-row arrays and can exhaust memory well inside the wall clock. Gating range spans statically is brittle; the address-space ceiling is the real backstop, applied with `RLIMIT_AS` in the child. Without it, "one request, not the whole service" is not true — with `--workers 1` an OOM takes down every request the instance is serving. Note Linux enforces `RLIMIT_AS`; macOS largely ignores it, so local runs do not exercise it. The timeout is enforced by running each evaluation in a **child process** (`runner.py`) that is killed when the budget expires — `signal.alarm` cannot do this, because FastAPI runs `def` path operations in its threadpool and signals are only deliverable on the main thread.

**The child is isolation, not a sandbox.** Forking buys blast-radius containment for *bugs* — a crash, a hang, an allocation blow-up. It is not defence in depth: no seccomp profile, no separate network namespace, same filesystem and environment as the parent. That is an accepted bet because `formulas` evaluates a formula language with no code-execution surface, and because the service holds no credentials worth reaching. If the engine ever gains a scripting escape, this is the assumption to revisit first.

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

**`max-concurrency=1` is load-bearing wherever this is deployed, not a default to leave alone.** Each request forks a child allowed 2 GiB of address space and the server runs `--workers 1`. Raising concurrency puts two such children on one instance and makes the "one request, not the whole service" claim false — an OOM would then take out a request that did nothing wrong. Size the instance's memory limit accordingly.

Cold starts pay for importing scipy/numpy/schedula, so a scale-to-zero deployment adds several seconds to the first request. Set the platform's own request timeout comfortably **above** `FORMULA_EVALUATOR_TIMEOUT`, so a slow evaluation surfaces as the 504 that names the budget rather than as a platform timeout that cannot say why.
