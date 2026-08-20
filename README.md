# Agentic Fuzzing of tomlc17

This repository contains the implementation and experimental artifacts for the
Agentic Fuzzing assignment targeting **tomlc17** (TOML 1.1, pinned commit
`75565ea`).

An LLM turns a formal TOML grammar into a [Hypothesis](https://hypothesis.readthedocs.io/)
generator; generated documents are executed against a sanitizer-built C harness;
results are triaged and deduplicated; and an adaptive feedback layer selects
proxy signals (no coverage instrumentation) that steer the LLM to refine the
generator across five iterations.

## Target

tomlc17 source is included under `tomlc17-source/`, pinned to commit `75565ea`
(release R260618). Do not build against upstream `master` — use the pinned
source, so the grammar and the parser agree (see *Challenges* in the report for
the commit-mismatch episode).

## Main pipeline

```
ANTLR grammar
  → LLM (Gemini 3.1 Flash-Lite) → Hypothesis generator
  → generator validation (static + executable gate)
  → 500-input campaign
  → tomlc17 sanitizer harness (ASan + UBSan)
  → runner: classify VALID / REJECT / SANITIZER / TIMEOUT / ABNORMAL
  → triage + dedup
  → feedback aggregation (k-path diversity · rejection taxonomy · crash signatures)
  → LLM refinement
  → next generator
```

The proxy signal that steers refinement — a novelty-biased adaptive selector
over those three sources, held stable by a validity guardrail — is the graded
core of the design and is described in full in the report.

## Main components

### Orchestration
- `loop.py` — the five-iteration agentic refinement loop; depth/malformed knobs,
  elitist refinement, stop conditions.
- `strategy_generator.py` — seed-prompt assembly and refinement-section
  construction; calls the LLM.
- `llm_client.py` — Gemini API client.
- `budget.py` — token and cost accounting against the ~$5 cap.

### Generation and validation
- `validate_generator.py` — generator validation gate (import → `SearchStrategy`
  → `.example()` → valid-path acceptance rate).
- `capability_check.py` — capability / regression checking (fixed-plumbing
  preservation; advisory vs catastrophic loss).
- `campaign.py` — runs the 500-input `@given` campaign and Hypothesis shrinking.

### Execution and triage
- `runner.py` — target execution and outcome classification (5s timeout;
  sanitizer + fatal-signal detection, POSIX and Windows).
- `triage.py` — tomlc17 rejection taxonomy (~30 categories) and crash
  signatures.
- `stack.py` — stack normalization, recursion folding, and LCS similarity.
- `crash_db.py` — per-signature crash deduplication.

### Feedback
- `feedback.py` — feedback aggregation and the adaptive priority ladder; fair
  frequency baseline.
- `structural_metrics.py` — input-side structural measurements (nesting depth,
  container size, dotted-key parts).
- `malformed_tracker.py` — rejection-category exploration tracking; reached vs
  never-reached reporting.

### Artifacts
- `strategies/` — the generated V1–V5 generators.
- `grammar/` — the adapted ANTLR grammar (`TomlLexer.g4`, `TomlParser.g4`) and
  adaptation notes.
- `harness/` — the sanitizer-enabled C harness, build script, and
  `ubsan.ignore`.
- `logs/` — per-iteration and evolution logs, baseline result, and category
  coverage.

## Results

The five-iteration campaign completed V1–V5. **No sanitizer-confirmed tomlc17
crash was discovered.** The generator reached nesting depth 58 and 18 of 32
rejection categories; every parser limit that was hit (depth, container size,
key parts) was cleanly rejected (exit 1), not crashed.

A **positive control** (Positive controls confirm that the sanitizer/crash-detection pipeline fires on planted faults, built with the harness's exact flags) confirms the
detection pipeline fires on real faults — so the null result reflects the
target, not a silent detector. Detailed iteration and evolution logs are under
`logs/`.

## Baseline

The deliberately naive baseline (random / lightly-structured text, no grammar
awareness) was run to validate the pipeline end-to-end before any LLM spend.

Run: 100 inputs, seed 1701.

Result:
- REJECT: 98
- VALID: 2

Result file: `logs/baseline.json`

The near-total rejection rate is the point: it confirms the pipeline detects and
logs outcomes correctly, and it is the comparison point for the grammar-driven
generator (which reaches a valid-path acceptance rate near 1.0).

## Reproduction

Requires Python 3.12, a clang toolchain with ASan/UBSan (developed on
MSYS2 clang64), and a `GEMINI_API_KEY` in the environment for the LLM steps.

```bash
# 1. Set up the environment
python -m venv .venv312
.venv312/Scripts/activate        # Windows/MSYS2
pip install -r requirements.txt

# 2. Build the sanitizer harness against the pinned tomlc17 source
bash harness/build_toml.sh
#   → produces harness/toml_harness.exe (ASan + UBSan, page_create ignorelisted)

# 3. Baseline (pipeline demonstration, no LLM spend)
python baseline_generator.py
#   → writes logs/baseline.json

# 4. Full five-iteration agentic run
export GEMINI_API_KEY=...        # required for generation/refinement
python loop.py 2>&1 | tee logs/full_loop.log
#   → generates strategies/generator_v1.py … generator_v5.py
#   → writes logs/iteration_*.json and logs/evolution.json

```markdown
# 5. Positive control (verify sanitizer/crash detection)

A planted heap-buffer-overflow is included under `harness/` to verify that
the sanitizer-enabled execution pipeline detects a known memory-safety fault.

```bash
./harness/crashtest.exe
```

Notes:
- Each unit module is runnable standalone for inspection, e.g.
  `python triage.py`, `python structural_metrics.py`,
  `python malformed_tracker.py`, `python capability_check.py strategies/generator_v5.py`.
- The run is bounded to 5 iterations / ~$5 LLM spend / 500 examples per
  iteration / 5s per input; token and cost totals are logged.
