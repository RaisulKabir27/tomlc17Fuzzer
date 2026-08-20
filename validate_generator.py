"""
validate_generator.py — Pre-campaign generator validation gate.

MEASURES VALID-PATH ACCEPTANCE RATE, NOT OVERALL ACCEPTANCE RATE.

Overall acceptance rate mixes two populations with opposite goals: the valid
path, which SHOULD be accepted, and the malformed path, which SHOULD be
rejected. A drop in the mixture is ambiguous — it could mean malformed
exploration is working as designed, or that the valid generator is broken —
and those two readings call for opposite corrections.

Valid-path acceptance rate has an unambiguous target: it should approach
1.0. Any shortfall is a defect in the valid generator with an actionable
fix, not a trade-off to balance. That is what makes it usable as a steering
signal rather than a trend to watch.

The valid path is isolated by reloading the generator module with
MALFORMED_WEIGHT forced to 0.0, so no draw is routed to the malformed
branch. Generators that ignore the knob are detected and reported rather
than silently mismeasured.

The gate also records which structural features actually appeared in the
sample. A required strategy that produces nothing across the whole sample
is an objective defect — invisible to acceptance rate alone, since a
generator missing its deep-nesting branch can still look perfectly healthy.
"""

import ast
import importlib
import sys
import tempfile
from collections import Counter
from pathlib import Path

from runner import run_harness
from triage import triage

import structural_metrics

# Valid-path floor. A generator whose *valid* branch is rejected more often
# than this is emitting invalid TOML by construction, not exploring
# boundaries. Set deliberately low: the gate catches broken generators, it
# does not rank working ones.
VALID_PATH_FLOOR = 0.60

# Overall-rate floor, retained for reporting only. Never gates anything.
ACCEPTANCE_FLOOR = 0.15

# 50 rather than 20: at 20 samples a branch that fires 5% of the time is
# missed about a third of the time, and branch-firing detection is one of
# the things this gate now exists to do.
SMOKE_TEST_SIZE = 50


def _run_sample(strategy, n, tmpdir, label):
    """Draw n examples, run each through the harness, tally outcomes."""
    accepted = 0
    rejected = 0
    crashed = 0
    encoding_errors = 0
    harness_errors = 0
    rejection_categories = Counter()
    texts = []

    for i in range(n):
        try:
            text = strategy.example()
        except Exception as e:
            return None, {
                "error": (
                    f"strategy.example() raised during {label}: "
                    f"{type(e).__name__}: {e}"
                ),
                "passed": False,
            }

        if not isinstance(text, str):
            text = str(text)
        texts.append(text)

        input_file = tmpdir / f"{label}_{i:04d}.toml"
        try:
            input_file.write_text(
                text, encoding="utf-8", errors="surrogatepass"
            )
        except UnicodeEncodeError:
            encoding_errors += 1
            continue

        report = triage(run_harness(str(input_file)))
        term = report.get("termination")

        if term == "NORMAL":
            accepted += 1
        elif term == "REJECT":
            rejected += 1
            cat = (report.get("rejection") or {}).get("error_type", "UNKNOWN")
            rejection_categories[cat] += 1
        elif term in ("SANITIZER", "TIMEOUT", "ABNORMAL"):
            crashed += 1
        elif term == "HARNESS_ERROR":
            harness_errors += 1

    non_crash = accepted + rejected
    return {
        "accepted": accepted,
        "rejected": rejected,
        "crashed": crashed,
        "encoding_errors": encoding_errors,
        "harness_errors": harness_errors,
        "rate": (accepted / non_crash) if non_crash else 0.0,
        "top_rejections": rejection_categories.most_common(5),
        "texts": texts,
    }, None


def knob_is_wired(source):
    """Whether MALFORMED_WEIGHT is actually READ, not merely assigned.

    A generator can declare the constant and then implement its ratio some
    other way — for example `st.one_of([valid] * 9 + [malformed])`. The knob
    is then decorative: the loop writes a value each iteration that changes
    nothing, and the valid path cannot be isolated by setting it to zero.

    Returns (assigned: bool, read: bool).
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return False, False

    assigned = False
    read = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == "MALFORMED_WEIGHT":
            if isinstance(node.ctx, ast.Store):
                assigned = True
            elif isinstance(node.ctx, ast.Load):
                read = True
    return assigned, read


def load_valid_only_strategy(version):
    """Import generator_v{version} with MALFORMED_WEIGHT forced to 0.0.

    Returns (strategy, knob_wired). knob_wired is False when the constant is
    absent or is assigned but never read — in either case the valid path
    cannot be isolated, and the caller must not treat the resulting rate as
    a valid-path measurement.
    """
    module_name = f"generator_v{version}"
    if "strategies" not in sys.path:
        sys.path.insert(0, "strategies")
    importlib.invalidate_caches()
    if module_name in sys.modules:
        del sys.modules[module_name]

    source = (Path("strategies") / f"{module_name}.py").read_text(
        encoding="utf-8"
    )
    assigned, read = knob_is_wired(source)

    if not (assigned and read):
        module = importlib.import_module(module_name)
        return module.toml_documents, False

    # Re-execute the source with the constant pinned to 0. Setting the
    # attribute after import is not enough: toml_documents is built at
    # import time, so the original value is already baked into the strategy.
    patched = []
    for line in source.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("MALFORMED_WEIGHT") and "=" in stripped:
            indent = line[: len(line) - len(stripped)]
            patched.append(f"{indent}MALFORMED_WEIGHT = 0.0")
        else:
            patched.append(line)

    namespace = {"__name__": f"{module_name}__validonly"}
    exec(
        compile("\n".join(patched), f"{module_name}.py", "exec"),
        namespace,
    )
    return namespace["toml_documents"], True


def measure_branch_coverage(texts):
    """Structural features actually observed in the sample.

    A generator can score a healthy acceptance rate while an entire
    exploration vector silently never fires. These flags make that visible.
    """
    if not texts:
        return {}

    max_depth = 0
    max_container = 0
    max_key_parts = 0
    saw_array = False
    saw_inline_table = False
    saw_table_header = False
    saw_array_table = False
    saw_multiline = False
    saw_escape = False
    saw_datetime_like = False

    for t in texts:
        try:
            max_depth = max(max_depth, structural_metrics.max_nesting_depth(t))
            max_container = max(
                max_container, structural_metrics.max_container_size(t)
            )
            max_key_parts = max(
                max_key_parts, structural_metrics.max_dotted_key_parts(t)
            )
        except Exception:
            pass

        if "[" in t:
            saw_array = True
        if "{" in t:
            saw_inline_table = True
        if "\n[" in t or t.startswith("["):
            saw_table_header = True
        if "[[" in t:
            saw_array_table = True
        if '"""' in t or "'''" in t:
            saw_multiline = True
        if "\\" in t:
            saw_escape = True
        if "-" in t and ":" in t:
            saw_datetime_like = True

    return {
        "max_depth": max_depth,
        "max_container": max_container,
        "max_dotted_key_parts": max_key_parts,
        "saw_array": saw_array,
        "saw_inline_table": saw_inline_table,
        "saw_table_header": saw_table_header,
        "saw_array_table": saw_array_table,
        "saw_multiline_string": saw_multiline,
        "saw_escape": saw_escape,
        "saw_datetime_like": saw_datetime_like,
    }


def validate_generator(
    strategy,
    n=SMOKE_TEST_SIZE,
    floor=VALID_PATH_FLOOR,
    valid_only_strategy=None,
    knob_wired=True,
):
    """Smoke-test a generator. Returns (passed, stats).

    When valid_only_strategy is supplied, the gate decision is made on the
    VALID-PATH rate measured from it. Otherwise it falls back to the overall
    rate and marks the result as unisolated, so a caller cannot mistake a
    mixture for a valid-path measurement.
    """
    with tempfile.TemporaryDirectory(prefix="toml_validate_") as td:
        tmpdir = Path(td)

        overall, err = _run_sample(strategy, n, tmpdir, "mixed")
        if err:
            return False, err

        valid_path = None
        if valid_only_strategy is not None and knob_wired:
            valid_path, err = _run_sample(
                valid_only_strategy, n, tmpdir, "validonly"
            )
            if err:
                return False, err

        branches = measure_branch_coverage(overall["texts"])

    isolated = valid_path is not None
    gate_rate = valid_path["rate"] if isolated else overall["rate"]
    passed = gate_rate >= floor

    stats = {
        "valid_path_isolated": isolated,
        "knob_wired": knob_wired,
        "gate_rate": round(gate_rate, 3),
        "floor": floor,
        "passed": passed,
        "total_generated": n,
        # Overall mixture — reported, never gated on.
        "overall_accepted": overall["accepted"],
        "overall_rejected": overall["rejected"],
        "overall_crashed": overall["crashed"],
        "acceptance_rate": round(overall["rate"], 3),
        "encoding_errors": overall["encoding_errors"],
        "harness_errors": overall["harness_errors"],
        "top_rejections": overall["top_rejections"],
        "branches": branches,
    }

    if isolated:
        stats.update({
            "valid_path_accepted": valid_path["accepted"],
            "valid_path_rejected": valid_path["rejected"],
            "valid_path_rate": round(valid_path["rate"], 3),
            "valid_path_top_rejections": valid_path["top_rejections"],
        })

    # Missing structural features are objective defects worth surfacing even
    # when the acceptance rate looks fine.
    missing = [
        name for name, seen in branches.items()
        if name.startswith("saw_") and seen is False
    ]
    if missing:
        stats["missing_features"] = missing

    return passed, stats


def validate_version(version, n=SMOKE_TEST_SIZE, floor=VALID_PATH_FLOOR):
    """Convenience wrapper: load generator_v{version} both ways and gate it."""
    module_name = f"generator_v{version}"
    if "strategies" not in sys.path:
        sys.path.insert(0, "strategies")
    importlib.invalidate_caches()
    if module_name in sys.modules:
        del sys.modules[module_name]
    module = importlib.import_module(module_name)

    try:
        valid_only, knob_wired = load_valid_only_strategy(version)
    except Exception as e:
        print(f"  (could not isolate valid path: {e})", flush=True)
        valid_only, knob_wired = None, False

    return validate_generator(
        module.toml_documents,
        n=n,
        floor=floor,
        valid_only_strategy=valid_only,
        knob_wired=knob_wired,
    )


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser()
    p.add_argument("--version", type=int, default=1)
    p.add_argument("--n", type=int, default=SMOKE_TEST_SIZE)
    args = p.parse_args()

    passed, stats = validate_version(args.version, n=args.n)

    print("=== GENERATOR VALIDATION GATE ===")
    print(f"Result: {'PASS' if passed else 'FAIL'}")

    if stats.get("error"):
        print(f"Error: {stats['error']}")
        sys.exit(1)

    if stats["valid_path_isolated"]:
        print(f"\nVALID-PATH (MALFORMED_WEIGHT forced to 0) — gated on this:")
        print(f"  accepted: {stats['valid_path_accepted']}"
              f"/{stats['total_generated']}")
        print(f"  rate:     {stats['valid_path_rate']} "
              f"(floor {stats['floor']})")
        if stats.get("valid_path_top_rejections"):
            print("  rejections in the VALID path — these are defects:")
            for cat, c in stats["valid_path_top_rejections"]:
                print(f"    {cat}: {c}")
    else:
        print("\nWARNING: valid path could not be isolated. "
              "MALFORMED_WEIGHT is absent, or assigned but never read — "
              "the ratio is implemented some other way, so the constant "
              "the loop sets does nothing. Gated on the overall mixture, "
              "which is NOT a valid-path measurement.")

    print(f"\nOverall mixture (reported only): "
          f"{stats['acceptance_rate']}")

    b = stats.get("branches", {})
    if b:
        print(f"\nStructural reach in sample: depth {b.get('max_depth')}, "
              f"container {b.get('max_container')}, "
              f"dotted-key parts {b.get('max_dotted_key_parts')}")
    if stats.get("missing_features"):
        print(f"Features never seen: {stats['missing_features']}")
