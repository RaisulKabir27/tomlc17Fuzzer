"""
capability_check.py — Capability regression detection.

Inspects a generated generator's SOURCE (via AST) to verify that the
structural capabilities the seed prompt requires are still present.

Motivation: the V1 -> V2 refinement silently dropped the deep-nesting and
large-container malformed strategies. The refinement prompt says "preserve
what works", but nothing verified it, and no signal in the feedback ladder
could see the loss. This closes that gap.

This is static inspection of OUR OWN generated code — nothing about the
target is examined, so it carries no black-box implications.

The check reports; it does not block. A dropped capability becomes feedback
for the next iteration.
"""

import ast
from pathlib import Path


# Function names the seed prompt's skeleton requires, and what each provides.
# Fixed-plumbing functions that must survive verbatim. These are mechanism,
# not TOML content: losing one removes the generator's ability to reach depth,
# size, or uniqueness at all, which no amount of feedback can restore.
FIXED_PLUMBING = {
    "_unique_names": "unique key/table names (prevents accidental collisions)",
    "_nest": "drawn nesting depth (st.recursive alone cannot reach depth)",
    "_toml_documents": "valid/malformed routing via MALFORMED_WEIGHT",
}

REQUIRED_CAPABILITIES = {
    "valid_leaf": "valid scalar values (the recursive base case)",
    "valid_value": "recursive arrays and inline tables",
    "valid_document": "complete valid documents",
    "_malformed_document": "the malformed dispatch harness",
    "_toml_documents": "the top-level valid/malformed router",
}

# Malformed strategies the skeleton enumerates. Losing one silently removes
# an exploration vector.
EXPECTED_MALFORMED = [
    "_malformed_duplicate_key",
    "_malformed_dotted_key_dup",
    "_malformed_inline_table_ext",
    "_malformed_table_conflict",
    "_malformed_array_table_conflict",
    "_malformed_int_overflow",
    "_malformed_float_overflow",
    "_malformed_deep_nesting",
    "_malformed_long_dotted_key",
]

# _malformed_large_container was removed from the required set. Requiring it
# produced a MemoryError (`", ".join(l * 16385)`) that killed one iteration,
# then a rejection deadlock that killed two more: the model could not
# implement it within Hypothesis's constraints, and the checker refused every
# candidate that omitted it. CONTAINER_TOO_LARGE had already been reached by
# then, so the vector cost three campaigns and bought nothing.

# Fraction of malformed strategies that must survive a refinement. Below
# this, the candidate is rejected outright; above it, the loss is reported
# as feedback and the campaign still runs. Rejecting a candidate costs a
# whole iteration, which is 20% of the budget, so only catastrophic loss
# (e.g. 14 -> 1) justifies it — a single dropped strategy does not.
CATASTROPHIC_LOSS_FRACTION = 0.5

# Constructs the seed prompt forbids.
FORBIDDEN_IMPORTS = {"random"}


def _defined_names(tree):
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
    return names


def _imported_modules(tree):
    mods = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                mods.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                mods.add(node.module.split(".")[0])
    return mods


def _uses_st_recursive(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == "recursive":
            return True
    return False


def _malformed_weight(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "MALFORMED_WEIGHT":
                    try:
                        return ast.literal_eval(node.value)
                    except Exception:
                        return None
    return None


def check_capabilities(path):
    """Inspect a generator module. Returns a dict of findings."""
    path = Path(path)
    source = path.read_text(encoding="utf-8")

    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        return {"parsed": False, "error": str(e)}

    names = _defined_names(tree)
    imports = _imported_modules(tree)

    missing_plumbing = [n for n in FIXED_PLUMBING if n not in names]

    missing_core = [
        n for n in REQUIRED_CAPABILITIES if n not in names
    ]
    present_malformed = [n for n in EXPECTED_MALFORMED if n in names]
    missing_malformed = [n for n in EXPECTED_MALFORMED if n not in names]

    # Any _malformed_* the model invented beyond the enumerated set.
    extra_malformed = sorted(
        n for n in names
        if n.startswith("_malformed_") and n not in EXPECTED_MALFORMED
        and n != "_malformed_document"
    )

    return {
        "parsed": True,
        "path": str(path),
        "missing_plumbing": missing_plumbing,
        "missing_core": missing_core,
        "present_malformed": present_malformed,
        "missing_malformed": missing_malformed,
        "extra_malformed": extra_malformed,
        "malformed_count": len(present_malformed) + len(extra_malformed),
        "uses_st_recursive": _uses_st_recursive(tree),
        "forbidden_imports": sorted(imports & FORBIDDEN_IMPORTS),
        "malformed_weight": _malformed_weight(tree),
        "defines_toml_documents": "toml_documents" in names,
    }


def compare(current, previous):
    """Diff two capability reports. Returns a list of regression strings.

    Kept for backward compatibility; classify() is the graded interface.
    """
    catastrophic, advisory = classify(current, previous)
    return catastrophic + advisory


def classify(current, previous):
    """Split regressions into (catastrophic, advisory).

    Catastrophic regressions justify rejecting the candidate and spending a
    retry. Advisory ones are reported as feedback while the campaign still
    runs, because rejecting costs a full iteration and a partial loss still
    leaves a generator worth measuring.
    """
    if not (current.get("parsed") and previous and previous.get("parsed")):
        return [], []

    catastrophic = []
    advisory = []

    prev_n = previous["malformed_count"]
    cur_n = current["malformed_count"]
    lost = set(previous["present_malformed"]) - set(current["present_malformed"])

    lost_plumbing = [
        n for n in current.get("missing_plumbing", [])
        if n not in previous.get("missing_plumbing", [])
    ]
    if lost_plumbing:
        catastrophic.append(
            "fixed plumbing removed: "
            + ", ".join(f"{n} ({FIXED_PLUMBING[n]})" for n in lost_plumbing)
            + ". This block is copy-verbatim mechanism; without it the "
            "generator cannot reach depth, size, or key uniqueness at all."
        )

    # Flattened recursion removes the generator's core capability.
    if previous["uses_st_recursive"] and not current["uses_st_recursive"]:
        catastrophic.append(
            "st.recursive is no longer used; recursion was flattened"
        )

    if prev_n and cur_n < prev_n * CATASTROPHIC_LOSS_FRACTION:
        catastrophic.append(
            f"malformed strategy count collapsed from {prev_n} to {cur_n} "
            f"(below {CATASTROPHIC_LOSS_FRACTION:.0%} of the previous version)"
        )
        if lost:
            catastrophic.append(
                "strategies dropped: " + ", ".join(sorted(lost))
            )
    else:
        if lost:
            advisory.append(
                "malformed strategies dropped since the previous version: "
                + ", ".join(sorted(lost))
                + ". Reinstate them; each is an exploration vector the "
                "campaign cannot otherwise reach."
            )
        if cur_n < prev_n:
            advisory.append(
                f"malformed strategy count fell from {prev_n} to {cur_n}"
            )

    return catastrophic, advisory


def build_instruction(report, regressions=None):
    """Render capability findings as a refinement instruction."""
    if not report.get("parsed"):
        return ""

    lines = []

    if report.get("missing_plumbing"):
        lines.append(
            "FIXED PLUMBING MISSING: "
            + ", ".join(
                f"{n} ({FIXED_PLUMBING[n]})"
                for n in report["missing_plumbing"]
            )
            + ". Copy this block verbatim from the specification. It is "
            "mechanism, not content — rewriting it is what removes the "
            "generator's ability to reach depth and size."
        )

    if report["missing_core"]:
        lines.append(
            "MISSING REQUIRED STRUCTURE: "
            + ", ".join(
                f"{n} ({REQUIRED_CAPABILITIES[n]})"
                for n in report["missing_core"]
            )
            + ". Restore these; the skeleton depends on them."
        )

    if report["missing_malformed"]:
        lines.append(
            "MISSING MALFORMED STRATEGIES: "
            + ", ".join(report["missing_malformed"])
            + ". Each absent strategy is an exploration vector the campaign "
            "cannot reach at all. Reinstate them."
        )

    if regressions:
        lines.append("CAPABILITY REGRESSION: " + "; ".join(regressions) + ".")

    if report["forbidden_imports"]:
        lines.append(
            "FORBIDDEN IMPORT: "
            + ", ".join(report["forbidden_imports"])
            + ". All variation must come from Hypothesis strategies."
        )

    if not report["uses_st_recursive"]:
        lines.append(
            "st.recursive is absent. Arrays and inline tables must nest "
            "recursively rather than being flattened."
        )

    return "\n".join(lines)


if __name__ == "__main__":
    import sys

    target = sys.argv[1] if len(sys.argv) > 1 else "strategies/generator_v1.py"
    if not Path(target).exists():
        print(f"No such file: {target}")
        print("Usage: python capability_check.py strategies/generator_v2.py")
        sys.exit(0)

    report = check_capabilities(target)
    if not report["parsed"]:
        print(f"Could not parse: {report['error']}")
        sys.exit(1)

    print(f"=== CAPABILITY CHECK: {report['path']} ===")
    print(f"toml_documents defined : {report['defines_toml_documents']}")
    print(f"uses st.recursive      : {report['uses_st_recursive']}")
    print(f"MALFORMED_WEIGHT       : {report['malformed_weight']}")
    print(f"malformed strategies   : {report['malformed_count']}")
    if report["missing_core"]:
        print(f"MISSING CORE           : {report['missing_core']}")
    if report["missing_malformed"]:
        print(f"MISSING MALFORMED      : {report['missing_malformed']}")
    if report["extra_malformed"]:
        print(f"extra malformed        : {report['extra_malformed']}")
    if report["forbidden_imports"]:
        print(f"FORBIDDEN IMPORTS      : {report['forbidden_imports']}")

    instruction = build_instruction(report)
    if instruction:
        print("\n--- instruction ---")
        print(instruction)
