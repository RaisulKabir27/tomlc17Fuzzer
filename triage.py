from crash_db import CrashDatabase
from stack import extract_stack, normalize_stack
import re

db = CrashDatabase()

# Terminations that represent an actual crash (kept in sync with feedback.py).
CRASH_TERMINATIONS = {"SANITIZER", "TIMEOUT", "ABNORMAL"}

# Number of top (recursion-folded) frames to hash into the crash signature.
# Research standard for stack-hash dedup is N = 3..5.
SIGNATURE_FRAMES = 5


# ============================================================================
# Rejection taxonomy - tomlc17-specific.
#
# Categories derived from tomlc17's ACTUAL error strings (extracted from
# tomlc17.c). The harness prints "reject: <errmsg>", where errmsg may carry a
# "(line N) " prefix, so we match on substrings of the full text.
#
# Ordered most-specific first; first match wins. Refine/rename to suit your
# proxy-signal design - HOW rejection categories steer the loop is your Phase-4
# call; this is just the tomlc17 vocabulary.
# ============================================================================
_REJECT_RULES = [
    # --- depth / size / resource limits (boundary behavior) ---
    ("stack overflow",                  "STACK_OVERFLOW"),
    ("too many key parts",              "TOO_MANY_KEY_PARTS"),
    ("too large",                       "CONTAINER_TOO_LARGE"),
    ("out of memory",                   "OUT_OF_MEMORY"),          # watch-item
    # --- numeric ---
    ("error parsing float",             "FLOAT_PARSE"),
    ("error parsing integer",           "INT_PARSE"),
    ("invalid boolean",                 "INVALID_BOOLEAN"),
    # --- strings / escapes / encoding ---
    ("bad escape char",                 "BAD_ESCAPE"),
    ("hex digits after",                "BAD_HEX_ESCAPE"),
    ("invalid char in string",          "INVALID_STRING_CHAR"),
    ("unterminated string",             "UNTERMINATED_STRING"),
    ("unterminated",                    "UNTERMINATED"),
    ("bad control char in comment",     "BAD_CONTROL_CHAR_COMMENT"),
    ("invalid utf8",                    "UTF8_ERROR"),
    ("converting ucs",                  "UTF8_ERROR"),
    # --- date / time ---
    ("invalid date",                    "INVALID_DATETIME"),
    ("invalid time",                    "INVALID_DATETIME"),
    ("invalid timestamp",               "INVALID_DATETIME"),
    ("invalid timezone",                "INVALID_DATETIME"),
    # --- semantic / document-state (grammar-valid but tomlc17-invalid) ---
    ("duplicate key",                   "DUPLICATE_KEY"),
    ("table defined before",            "TABLE_CONFLICT"),
    ("table defined more than once",    "TABLE_CONFLICT"),
    ("inline table cannot be extended", "INLINE_TABLE_EXTENSION"),
    ("cannot extend a static array",    "CANNOT_EXTEND_ARRAY"),
    ("entry must be an array",          "ARRAY_TABLE_MISMATCH"),
    ("must be array of tables",         "ARRAY_TABLE_MISMATCH"),
    ("cannot locate table",             "TABLE_LOCATION"),
    ("expects a string in dotted-key",  "DOTTED_KEY_TYPE"),
    # --- syntax ---
    ("expect '='",                      "EXPECT_EQUALS"),
    ("missing '='",                     "EXPECT_EQUALS"),
    ("unexpected newline",              "UNEXPECTED_NEWLINE"),
    ("unexpected comma",                "COMMA_ERROR"),
    ("missing comma",                   "COMMA_ERROR"),
    ("missing ']]'",                    "MISSING_BRACKET"),
    ("missing right-bracket",           "MISSING_BRACKET"),
    ("missing key",                     "MISSING_KEY"),
    ("missing value",                   "MISSING_VALUE"),
    ("invalid value",                   "INVALID_VALUE"),
    ("endl expected",                   "ENDL_EXPECTED"),
    ("while parsing array",             "ARRAY_PARSE"),
    # --- suspicious: a parser shouldn't hit these on mere malformed input ---
    ("internal error",                  "INTERNAL_ERROR"),         # watch-item
    ("internal:",                       "INTERNAL_ERROR"),         # watch-item
]


def extract_rejection_info(stderr, stdout):
    text = (stderr.strip() or stdout.strip())
    if not text:
        return {"error_type": "UNKNOWN", "raw_error": "", "context": None}

    lower = text.lower()
    error_type = "OTHER"
    for needle, category in _REJECT_RULES:
        if needle.lower() in lower:
            error_type = category
            break

    return {"error_type": error_type, "raw_error": text, "context": None}


# ---- source location (unchanged: prefer the tomlc17.c frame) ----
_LOC_RE = re.compile(r"([A-Za-z0-9_./\\:+-]+\.c):(\d+):(\d+)")


def extract_source_location(stderr):
    matches = _LOC_RE.findall(stderr)
    if not matches:
        return None

    def basename(p):
        return re.split(r"[/\\]", p)[-1]

    chosen = None
    for path, line, col in matches:
        if basename(path) == "tomlc17.c":
            chosen = (path, line, col)
            break
    if chosen is None:
        chosen = matches[0]

    path, line, col = chosen
    return {"file": basename(path), "line": int(line), "column": int(col)}


def fold_recursion(frames):
    """Collapse consecutive duplicate frames (direct recursion).

    Deep-recursion crashes produce stacks like
        parse_val -> parse_val -> parse_val -> ... -> crash
    where the depth varies per input. Without folding, each depth yields a
    DIFFERENT signature and one bug fragments into many. Folding makes the
    signature depth-independent. (Residual limitation: mutual recursion
    A->B->A->B is only partly collapsed; document this normalization choice.)
    """
    folded = []
    for f in frames:
        if not folded or folded[-1] != f:
            folded.append(f)
    return folded


def triage(result):
    stderr = result["stderr"]
    frames = extract_stack(result["stderr"])
    normalized_stack = normalize_stack(frames)

    report = {
        "status": result["status"],
        "exit_code": result["exit_code"],
        "timed_out": result["timed_out"],
        "termination": None,
        "sanitizers": [],
        "bug_type": None,
        "source_location": None,
        "signal": None,
        "stack": normalized_stack,
    }

    report["sanitizers"] = result.get("sanitizers", [])
    report["signal"] = result.get("signal")  # [NEW] fatal signal name, if any

    # ---- termination classification ----
    if result["timed_out"]:
        report["termination"] = "TIMEOUT"
    elif report["sanitizers"]:
        report["termination"] = "SANITIZER"
    elif result["exit_code"] == 0:
        report["termination"] = "NORMAL"
    elif result["exit_code"] == 1:
        report["termination"] = "REJECT"
        report["rejection"] = extract_rejection_info(
            result.get("stderr", ""), result.get("stdout", "")
        )
    elif result["exit_code"] == 2:
        report["termination"] = "HARNESS_ERROR"
    else:
        report["termination"] = "ABNORMAL"
        report["rejection"] = None

    # ---- bug type (from sanitizer text) ----
    for kind in (
        "heap-buffer-overflow", "heap-use-after-free", "stack-buffer-overflow",
        "global-buffer-overflow", "stack-overflow", "stack-use-after-return",
        "dynamic-stack-buffer-overflow", "attempting free on address", "SEGV",
        "signed-integer-overflow", "signed integer overflow",
        "shift", "index out of bounds", "null pointer",
    ):
        if kind in stderr:
            # Normalize to hyphenated form for consistent signatures.
            report["bug_type"] = kind.replace(" ", "-")
            break

    report["source_location"] = extract_source_location(stderr)
    report["signature"] = make_signature(report)

    # ---- only compare/store ACTUAL crashes ----
    if report["termination"] in CRASH_TERMINATIONS:
        report["similar_crashes"] = db.compare_with_existing(report)
        db.add_crash(report)
    else:
        report["similar_crashes"] = []

    return report


def make_signature(report):
    parts = [report["status"], report["bug_type"]]

    if report["sanitizers"]:
        parts.append("+".join(sorted(report["sanitizers"])))

    # [NEW] fold in the fatal signal name (SIGSEGV vs SIGFPE at the same site
    # are different bugs). Usually None for sanitizer crashes, which is fine.
    if report.get("signal"):
        parts.append(report["signal"])

    # Assignment: hash the top few frames. Fold recursion first so depth is
    # irrelevant, then take the top N frames nearest the crash.
    folded = fold_recursion(report["stack"])
    top = folded[:SIGNATURE_FRAMES]
    if top:
        parts.append(">".join(top))
    elif report["source_location"]:
        # Fallback when the stack couldn't be parsed (e.g. a stack-overflow
        # crash with corrupted frames - a documented stack-hash failure mode).
        loc = report["source_location"]
        parts.append(f'{loc["file"]}:{loc["line"]}')

    return "|".join(str(part) for part in parts)


if __name__ == "__main__":
    tests = [
        "reject: (line 1) error parsing float",
        "reject: (line 1) stack overflow",
        "reject: (line 2) duplicate key",
        "reject: (line 1) too many key parts",
        "reject: (line 1) bad control char in comment",
        "reject: (line 3) entry must be an array",
        "reject: (line 1) internal error",
        "reject: (line 1) some brand new message",
    ]
    print("REJECTION TAXONOMY TESTS")
    for t in tests:
        print(f"  {extract_rejection_info(t, '')['error_type']:24} <- {t}")
