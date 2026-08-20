"""
Deliberately naive baseline for the tomlc17 fuzzing pipeline.

Purpose:
    Exercise the existing runner/harness end-to-end with random /
    lightly-structured text, without using the TOML grammar, the
    agentic loop, feedback, or generator_v1.py.

Outputs:
    logs/baseline.json
"""

import json
import random
import string
import tempfile
from pathlib import Path

from runner import run_harness


NUM_CASES = 100
SEED = 1701
LOG_DIR = Path("logs")


def random_text(rng: random.Random) -> str:
    """Generate grammar-unaware random/lightly-structured text."""
    alphabet = string.ascii_letters + string.digits + string.punctuation + " \t"

    mode = rng.randrange(5)

    if mode == 0:
        # Completely random printable text.
        length = rng.randint(1, 160)
        return "".join(rng.choice(alphabet) for _ in range(length))

    if mode == 1:
        # Random key-looking text, but no TOML parsing knowledge.
        key = "".join(rng.choice(string.ascii_letters) for _ in range(rng.randint(1, 10)))
        value = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 80)))
        return f"{key} = {value}"

    if mode == 2:
        # Lightly structured bracket/brace noise.
        pieces = [
            "".join(rng.choice(alphabet) for _ in range(rng.randint(1, 30)))
            for _ in range(rng.randint(1, 5))
        ]
        wrappers = ["[", "]", "{", "}", "[[", "]]", '"', "'", ",", "="]
        return "".join(
            rng.choice(wrappers) if rng.random() < 0.30 else piece
            for piece in pieces
        )

    if mode == 3:
        # Random multi-line text.
        lines = []
        for _ in range(rng.randint(1, 8)):
            length = rng.randint(0, 60)
            lines.append("".join(rng.choice(alphabet) for _ in range(length)))
        return "\n".join(lines)

    # TOML-looking fragments without knowing the grammar.
    fragments = [
        "key =",
        "= value",
        "[",
        "]",
        "[[",
        "]]",
        "{",
        "}",
        '"unterminated',
        "'unterminated",
        "true false",
        "0x",
        "\\xGG",
        "@@@",
        "###",
    ]
    return rng.choice(fragments) + "".join(
        rng.choice(alphabet) for _ in range(rng.randint(0, 40))
    )


def main():
    rng = random.Random(SEED)
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    counts = {}
    cases = []

    with tempfile.TemporaryDirectory(prefix="tomlc17_baseline_") as tmp:
        tmpdir = Path(tmp)

        for i in range(NUM_CASES):
            text = random_text(rng)
            input_path = tmpdir / f"case_{i:04d}.toml"
            input_path.write_text(text, encoding="utf-8")

            result = run_harness(str(input_path))
            status = result["status"]
            counts[status] = counts.get(status, 0) + 1

            cases.append({
                "id": i,
                "input_length": len(text),
                "status": status,
                "exit_code": result["exit_code"],
                "signal": result["signal"],
                "timed_out": result["timed_out"],
                "sanitizers": result["sanitizers"],
                "stdout": result["stdout"],
                "stderr": result["stderr"],
                "input": text,
            })

    result = {
        "experiment": "naive_baseline",
        "description": (
            "Deliberately naive random/lightly-structured text with no "
            "grammar awareness, exercising the existing runner and "
            "tomlc17 sanitizer harness end-to-end."
        ),
        "num_cases": NUM_CASES,
        "seed": SEED,
        "harness": "runner.run_harness",
        "counts": counts,
        "cases": cases,
    }

    output = LOG_DIR / "baseline.json"
    output.write_text(
        json.dumps(result, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    print("Naive baseline complete.")
    print(f"Cases: {NUM_CASES}")
    print(f"Seed: {SEED}")
    print(f"Results: {output}")
    print("Counts:")
    for status in sorted(counts):
        print(f"  {status}: {counts[status]}")


if __name__ == "__main__":
    main()
