# Differential Validation Add-on

Put `differential_test.py` in the project root beside `runner.py` and `harness/`.
The semantic-invalid corpus belongs in `tomlc17_differential_validation/`.

Run from the project root:

    python differential_test.py

For the first pass without very large boundary inputs:

    python differential_test.py --skip-boundary

The script writes `differential_results.json`.

Interpretation:
- MATCH: both agree.
- GRAMMAR_MISMATCH: expected grammar result failed.
- MISMATCH: ANTLR and tomlc17 disagree.
- ANTLR_RUNTIME_ERROR: generated ANTLR runtime failed (known example: Python recursion on a huge array).
- TOMLC17_UNEXPECTED: tomlc17 did not match the expected corpus behavior.

Boundary cases are reported separately because implementation limits are
intentionally not encoded in the grammar.
