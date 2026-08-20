import sys
import time
from pathlib import Path

from hypothesis import strategies as st


def validate_strategy(strategy_file, examples=10, max_size=1_000_000):
    strategy_file = Path(strategy_file)

    if not strategy_file.exists():
        return False, ["Strategy file does not exist."]

    sys.path.insert(0, str(strategy_file.parent))

    module_name = strategy_file.stem

    try:
        module = __import__(module_name)
    except Exception as e:
        return False, [f"Import failed: {e}"]

    if not hasattr(module, "toml_documents"):
        return False, ["Missing top-level 'toml_documents' strategy."]

    strategy = module.toml_documents

    if not isinstance(strategy, st.SearchStrategy):
        return False, ["toml_documents is not a Hypothesis SearchStrategy."]

    problems = []

    start = time.monotonic()

    for i in range(examples):
        try:
            value = strategy.example()
        except Exception as e:
            problems.append(f"Example {i + 1} generation failed: {e}")
            continue

        if not isinstance(value, str):
            problems.append(
                f"Example {i + 1} is {type(value).__name__}, not str."
            )
            continue

        if len(value) > max_size:
            problems.append(
                f"Example {i + 1} is too large: {len(value)} characters."
            )

    elapsed = time.monotonic() - start

    print(f"Examples tested: {examples}")
    print(f"Generation time: {elapsed:.3f}s")

    if problems:
        print("VALIDATION: FAIL")
        for problem in problems:
            print("-", problem)
        return False, problems

    print("VALIDATION: PASS")
    return True, []


if __name__ == "__main__":
    validate_strategy("strategies/strategy_v1.py")