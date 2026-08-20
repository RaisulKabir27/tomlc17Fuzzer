"""
structural_metrics.py — Input-side structural measurement.

Measures properties of the TOML text the generator produced: nesting depth,
container size, dotted-key part count, document length. Feeds Step 4.4's
"some structural measure of diversity (e.g. distribution of nesting depth,
which grammar productions have and haven't appeared in generated inputs)".

BLACK-BOX NOTE:
    Every measurement here is taken on inputs WE generated. The target is
    never inspected, instrumented, or read. This is self-measurement of the
    generator's own output, which is what makes it a legitimate black-box
    proxy signal — the same footing as k-path diversity.

ACCURACY NOTE:
    Depth and size are computed by a bracket scanner that tracks string
    context, not by a full TOML parse. It is deliberately approximate:
    exotic escape/multiline combinations can mis-count. It is a
    distributional signal, not a parser. Document that in the report.
"""

from collections import Counter


def _scan(text):
    """Single pass over TOML text, tracking string context.

    Yields (char, depth_before, in_string) for characters outside strings,
    where depth counts open [ and { containers.
    """
    i = 0
    n = len(text)
    depth = 0
    in_basic = False
    in_literal = False
    in_ml_basic = False
    in_ml_literal = False

    while i < n:
        ch = text[i]

        # Multi-line delimiters first (longest match).
        if not (in_basic or in_literal or in_ml_literal) and text.startswith('"""', i):
            in_ml_basic = not in_ml_basic
            i += 3
            continue
        if not (in_basic or in_literal or in_ml_basic) and text.startswith("'''", i):
            in_ml_literal = not in_ml_literal
            i += 3
            continue

        if in_ml_basic or in_ml_literal:
            # Escapes inside multiline basic strings.
            if in_ml_basic and ch == "\\":
                i += 2
                continue
            i += 1
            continue

        if in_basic:
            if ch == "\\":
                i += 2
                continue
            if ch == '"':
                in_basic = False
            i += 1
            continue

        if in_literal:
            if ch == "'":
                in_literal = False
            i += 1
            continue

        # Outside any string.
        if ch == '"':
            in_basic = True
            i += 1
            continue
        if ch == "'":
            in_literal = True
            i += 1
            continue
        if ch == "#":
            # Comment to end of line.
            nl = text.find("\n", i)
            i = n if nl == -1 else nl
            continue

        if ch in "[{":
            yield ch, depth, False
            depth += 1
            i += 1
            continue
        if ch in "]}":
            depth = max(0, depth - 1)
            yield ch, depth, False
            i += 1
            continue

        yield ch, depth, False
        i += 1


def max_nesting_depth(text):
    """Deepest container nesting reached, ignoring brackets inside strings."""
    depth = 0
    best = 0
    for ch, d, _ in _scan(text):
        if ch in "[{":
            depth = d + 1
            best = max(best, depth)
        elif ch in "]}":
            depth = d
    return best


def max_container_size(text):
    """Largest element count in any single container (comma-separated).

    Counts separators at each depth and takes the maximum, so a 16k-element
    array registers even when nested. Approximate: a trailing comma inflates
    the count by one.
    """
    counts = {}
    best = 0
    for ch, d, _ in _scan(text):
        if ch in "[{":
            counts[d + 1] = 1          # an open container holds >= 1 element
        elif ch in "]}":
            best = max(best, counts.pop(d + 1, 0))
        elif ch == ",":
            if d in counts:
                counts[d] += 1
    for v in counts.values():
        best = max(best, v)
    return best


def max_dotted_key_parts(text):
    """Most dot-separated parts in any key, over key= lines and table headers."""
    best = 0
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue

        if line.startswith("["):
            key_part = line.strip("[]").strip()
        elif "=" in line:
            key_part = line.split("=", 1)[0].strip()
        else:
            continue

        if not key_part:
            continue

        # Count dots outside quotes.
        parts = 1
        in_b = in_l = False
        for ch in key_part:
            if ch == '"' and not in_l:
                in_b = not in_b
            elif ch == "'" and not in_b:
                in_l = not in_l
            elif ch == "." and not in_b and not in_l:
                parts += 1
        best = max(best, parts)
    return best


def _bucket(value, edges):
    """Label a value by which half-open interval it falls in."""
    for lo, hi, label in edges:
        if lo <= value < hi:
            return label
    return edges[-1][2]


DEPTH_BUCKETS = [
    (0, 1, "0"), (1, 3, "1-2"), (3, 6, "3-5"), (6, 11, "6-10"),
    (11, 21, "11-20"), (21, 28, "21-27"), (28, 34, "28-33"),
    (34, 10**9, "34+"),
]

SIZE_BUCKETS = [
    (0, 1, "0"), (1, 6, "1-5"), (6, 21, "6-20"), (21, 101, "21-100"),
    (101, 1001, "101-1000"), (1001, 16383, "1001-16382"),
    (16383, 16387, "16383-16386"), (16387, 10**9, "16387+"),
]


def measure_campaign(records):
    """Aggregate structural metrics over one campaign's records."""
    depths = []
    sizes = []
    key_parts = []
    lengths = []

    for rec in records:
        text = rec.get("input")
        if not isinstance(text, str):
            continue
        try:
            depths.append(max_nesting_depth(text))
            sizes.append(max_container_size(text))
            key_parts.append(max_dotted_key_parts(text))
            lengths.append(len(text))
        except Exception:
            continue

    if not depths:
        return {"inputs_measured": 0}

    return {
        "inputs_measured": len(depths),
        "depth": {
            "max": max(depths),
            "mean": round(sum(depths) / len(depths), 2),
            "distribution": dict(Counter(
                _bucket(d, DEPTH_BUCKETS) for d in depths
            )),
        },
        "container_size": {
            "max": max(sizes),
            "mean": round(sum(sizes) / len(sizes), 2),
            "distribution": dict(Counter(
                _bucket(s, SIZE_BUCKETS) for s in sizes
            )),
        },
        "dotted_key_parts": {
            "max": max(key_parts),
            "distribution": dict(Counter(key_parts)),
        },
        "document_length": {
            "max": max(lengths),
            "mean": round(sum(lengths) / len(lengths), 1),
        },
    }


def build_instruction(metrics, previous=None):
    """Render structural metrics as a refinement instruction.

    Reports what the generator actually reached and, when a previous
    campaign's metrics are supplied, whether reach expanded or contracted.
    Contraction is called out explicitly: a refinement that quietly drops
    structural capability is otherwise invisible to the feedback loop.
    """
    if not metrics or not metrics.get("inputs_measured"):
        return ""

    depth = metrics["depth"]
    size = metrics["container_size"]
    parts = metrics["dotted_key_parts"]

    lines = [
        "STRUCTURAL REACH (measured on the inputs this generator produced):",
        f"  nesting depth      max {depth['max']}, mean {depth['mean']}",
        f"  container size     max {size['max']}, mean {size['mean']}",
        f"  dotted-key parts   max {parts['max']}",
    ]

    if previous and previous.get("inputs_measured"):
        pd = previous["depth"]["max"]
        ps = previous["container_size"]["max"]
        regressions = []

        pp = previous.get("dotted_key_parts", {}).get("max", 0)
        if parts["max"] < pp:
            regressions.append(
                f"dotted-key depth fell from {pp} to {parts['max']}"
            )
        if depth["max"] < pd:
            regressions.append(f"nesting depth fell from {pd} to {depth['max']}")
        if size["max"] < ps:
            regressions.append(f"container size fell from {ps} to {size['max']}")
        if regressions:
            lines += [
                "",
                "REGRESSION: " + "; ".join(regressions) + ". The previous "
                "version reached further. Restore that reach — do not trade "
                "structural depth for a higher acceptance rate.",
            ]

    lines += [
        "",
        "The grammar places no bound on nesting depth, container size, "
        "or dotted-key depth. These maxima describe the generator's reach, "
        "not the format's.",
        (
            f"Preserve the current recursive-depth capability; do not push nesting "
            f"depth higher merely for the sake of a larger maximum."
        ),
        (
            "Prioritize NEW VALID structural combinations and grammar-path diversity. "
            "Use the recursive depth already demonstrated as a capability, but do not "
            "increase maximum nesting depth unless needed to reach a genuinely new "
            "structural combination. Vary how tables, arrays-of-tables, inline tables, "
            "dotted keys, and nested values are combined. Treat dotted-key/table-path "
            "depth as an independent structural dimension. Prefer breadth of valid "
            "structural combinations over greater maximum depth."
        ),
        "Do not trade one structural dimension for another.",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    samples = [
        'a = 1\nb = "x"',
        'deep = [[[[[1]]]]]',
        'x = { a = { b = { c = 1 } } }',
        'arr = [1, 2, 3, 4, 5, 6, 7]',
        'k.a.b.c.d = 1',
        's = "not [ a ] bracket"',
        "lit = 'also { not } counted'",
        'ml = """\nstill [ not ] counted\n"""',
        '# comment [ ignored ]\nreal = [1]',
    ]

    print("Per-sample measurement:")
    for s in samples:
        print(f"  depth={max_nesting_depth(s):2}  "
              f"size={max_container_size(s):2}  "
              f"parts={max_dotted_key_parts(s):2}  "
              f"{s[:38]!r}")

    records = [{"input": s} for s in samples]
    m = measure_campaign(records)
    print(f"\nCampaign: {m['inputs_measured']} inputs")
    print(f"  depth max {m['depth']['max']}  dist {m['depth']['distribution']}")
    print(f"  size  max {m['container_size']['max']}")

    # Regression detection, using the observed V2 numbers as "previous".
    prev = {"inputs_measured": 500,
            "depth": {"max": 11}, "container_size": {"max": 13}}
    print("\n--- instruction (with regression) ---")
    print(build_instruction(m, previous=prev))
