"""
strategy_generator.py — LLM prompt assembly and generated-code validation.

V1  : seed prompt (+ retry diagnosis if an earlier attempt failed) -> Gemini
V2+ : seed prompt + previous generator + five feedback sections   -> Gemini

The five refinement sections are passed separately and rendered into their
own tagged blocks rather than concatenated into one string. Two reasons:

  1. Flash-Lite attends better to demarcated blocks than to a single wall of
     instructions (strict structural demarcation), and a labelled block makes
     it visible when the model has responded to one section and ignored the
     rest.
  2. Section ordering encodes priority. Capability regressions come first:
     a dropped strategy is an exploration vector the campaign cannot reach at
     all, so restoring it precedes any other refinement.

Order sent to the model:
    1. CAPABILITY   — required structure missing or lost since last version
    2. SIGNALS      — the adaptive proxy-signal selector's instructions
    3. STRUCTURAL   — measured reach of the inputs actually generated
    4. MALFORMED    — rejection categories not yet reached (exploration)
    5. WEIGHT       — the MALFORMED_WEIGHT to set this iteration

Empty sections are omitted entirely, so a clean iteration sends a shorter
prompt than a troubled one.
"""

from pathlib import Path
import py_compile
import subprocess
import sys
import ast

""" from llm_client import GeminiClient """
from llm_client import GeminiClient


SEED_PROMPT_FILE = Path("Seed Prompt.txt")
OUTPUT_DIR = Path("strategies")

# Rough chars-per-token for logging prompt size. Only used for a printed
# estimate; real token counts come back from the API in result["usage"].
CHARS_PER_TOKEN = 4


# ---------------------------------------------------------------------------
# Prompt section assembly
# ---------------------------------------------------------------------------
def _block(tag, body):
    """Wrap a feedback section in a tagged block, or return '' if empty."""
    if not body:
        return ""
    if isinstance(body, (list, tuple)):
        body = "\n".join(str(x) for x in body)
    body = str(body).strip()
    if not body:
        return ""
    return f"\n<{tag}>\n{body}\n</{tag}>\n"


def build_refinement_sections(
    campaign_signals=None,
    malformed_feedback=None,
    structural_feedback=None,
    capability_feedback=None,
    malformed_weight=None,
    max_depth=None,
):
    """Render the five feedback sections into tagged blocks.

    Returns (sections_text, present) where `present` lists the section names
    actually included — useful for logging which evidence drove a version.
    """
    parts = []
    present = []

    if capability_feedback:
        parts.append(_block("CAPABILITY_REGRESSION", capability_feedback))
        present.append("capability")

    if campaign_signals:
        parts.append(_block("CAMPAIGN_SIGNALS", campaign_signals))
        present.append("signals")

    if structural_feedback:
        parts.append(_block("STRUCTURAL_REACH", structural_feedback))
        present.append("structural")

    if malformed_feedback:
        parts.append(_block("MALFORMED_EXPLORATION", malformed_feedback))
        present.append("malformed_exploration")

    if max_depth is not None:
        parts.append(_block(
            "MAX_DEPTH_SETTING",
            f"Set MAX_DEPTH = {int(max_depth)} in this iteration. This "
            "value is derived from the structural reach measured in the "
            "previous campaign. Change only that constant; the _nest() "
            "plumbing that consumes it must stay verbatim.",
        ))
        present.append("max_depth")

    if malformed_weight is not None:
        parts.append(_block(
            "MALFORMED_WEIGHT_SETTING",
            f"Set MALFORMED_WEIGHT = {malformed_weight:.2f} in this "
            "iteration. This value is derived from the validity rate "
            "observed in the previous campaign. Change only that constant; "
            "do not introduce any other probability value.",
        ))
        present.append("weight")

    return "".join(parts), present


_SECTION_GUIDE = """
--- HOW TO READ THE FEEDBACK ---

Each block below reports evidence from running the previous generator. They
are ordered by how directly they constrain this iteration:

  CAPABILITY_REGRESSION   required structure that is missing or was lost.
                          Restore it before making any other change: an
                          absent strategy is a vector the campaign cannot
                          reach at all.
  CAMPAIGN_SIGNALS        what the adaptive signal selector concluded from
                          the campaign.
  STRUCTURAL_REACH        how deep and how large the generated inputs
                          actually got, measured on those inputs.
  MALFORMED_EXPLORATION   parser rejection categories never yet triggered.
  MALFORMED_WEIGHT_SETTING  the valid/malformed ratio for this iteration.

Address every block that appears. Do not respond to only the first one.
"""

_REFINEMENT_RULES = """
--- REFINEMENT TASK ---

Produce the next generator iteration.

RULES:
1. valid_leaf() must remain VALID-ONLY. Never move overflow integers,
   1e999, or any parser-rejected value into it. This is the single most
   important architectural rule: valid_leaf() is the base case of the
   recursion, so anything invalid placed there contaminates every nested
   array and inline table.
2. Malformed values belong in the _malformed_* strategies, which are not
   reachable from the recursive valid path.
3. Preserve the fixed harness section verbatim, from _malformed_document
   through toml_documents = _toml_documents().
4. Do not import random. Do not add probability constants beyond
   MALFORMED_WEIGHT.
5. Preserve working strategies. Change what the feedback identifies; leave
   the rest of the architecture intact.
6. Never raise the acceptance rate by removing capability. Dropping
   nesting, escapes, datetimes, unicode, or a malformed strategy is a
   regression even if validity improves.
7. For very large TOML containers near parser implementation limits
   (for example around 16,384 elements), do NOT use st.lists() with
   min_size/max_size in the thousands. Hypothesis must not be asked to
   construct enormous Python lists. Instead, construct the final TOML
   string using bounded/compositional strategies or deterministic string
   construction, while keeping the generated strategy executable.

Use the Hypothesis version installed in the project environment.
For floating-point strategies, use `allow_infinity`, not `allow_inf`.

Hypothesis strategies must be deterministic and stable. Do not make strategy
generation depend on mutable global state, counters, random module state,
time, filesystem state, environment state, or side effects. Do not mutate
shared objects while generating examples. Repeated calls to
toml_documents.example() must be able to generate independently and
consistently. Avoid custom strategies or @st.composite functions that depend
on external state.

Do not use Python's `random` module anywhere inside Hypothesis strategies.
Do not call random.random(), random.choice(), random.randint(), or similar
functions during strategy generation. Use Hypothesis strategies such as
st.sampled_from(), st.integers(), st.randoms(), or strategy composition
instead. The generated strategy must be fully controlled by Hypothesis.

The generated module must be directly executable and importable, and must
define a Hypothesis SearchStrategy named `toml_documents`.

Return ONLY the complete Python source code for the generator.
Do not include Markdown fences, explanations, or commentary.
"""


# ---------------------------------------------------------------------------
# Generated-code validation
# ---------------------------------------------------------------------------
def _strip_fences(code):
    """Remove markdown fences Flash-Lite adds despite instructions."""
    code = code.strip()
    if not code.startswith("```"):
        return code
    lines = code.splitlines()
    if lines and lines[0].strip().startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _validate_module(output_file, version):
    """Validate syntax, importability, strategy type, and basic execution.

    Raises on failure; the caller decides whether that warrants a retry.
    """
    # Gate 1: valid Python source.
    py_compile.compile(str(output_file), doraise=True)

    # Gate 2-4:
    #   - module imports
    #   - toml_documents exists
    #   - toml_documents is a Hypothesis SearchStrategy
    #   - strategy can actually execute and produce examples
    subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; "
                "from hypothesis.strategies import SearchStrategy; "
                "sys.path.insert(0, 'strategies'); "
                f"import generator_v{version} as m; "
                "assert hasattr(m, 'toml_documents'), "
                "'module does not define toml_documents'; "
                "assert isinstance(m.toml_documents, SearchStrategy), "
                "'toml_documents is not a Hypothesis SearchStrategy'; "
                "examples = [m.toml_documents.example() for _ in range(3)]; "
                "assert all(isinstance(x, str) for x in examples), "
                "'toml_documents did not produce strings'"
            ),
        ],
        check=True,
        timeout=15,
    )

def validate_generated_source(source: str) -> list[str]:
    """Reject generated strategies that are structurally unsafe for Hypothesis."""
    errors = []

    try:
        tree = ast.parse(source)

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "random":
                        errors.append(
                            "generated strategy imports Python's random module"
                        )

            elif isinstance(node, ast.ImportFrom):
                if node.module == "random":
                    errors.append(
                        "generated strategy imports from Python's random module"
                    )

        # ... existing AST checks continue here ...

    except SyntaxError as e:
        return [f"syntax error: {e}"]

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue


                # st.zip() is not available in the installed Hypothesis version.
        if (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "zip"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "st"
        ):
            errors.append(
                "st.zip() is not available in this Hypothesis version; "
                "use st.tuples(...).map(...) instead"
            )

        # Detect st.lists(..., min_size=N) where N is impractically large.
        if (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "lists"
        ):
            for kw in node.keywords:
                if kw.arg == "min_size" and isinstance(kw.value, ast.Constant):
                    if isinstance(kw.value.value, int) and kw.value.value > 1024:
                        errors.append(
                            f"st.lists() has min_size={kw.value.value}; "
                            "large TOML containers must not be represented "
                            "as enormous Hypothesis Python lists"
                        )

        # Detect invalid st.floats(allow_nan=True, min_value=..., max_value=...)
        if (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "floats"
        ):
            kwargs = {
                kw.arg: kw.value
                for kw in node.keywords
                if kw.arg is not None
            }

            allow_nan = kwargs.get("allow_nan")
            has_bounds = (
                "min_value" in kwargs or
                "max_value" in kwargs
            )

            if (
                isinstance(allow_nan, ast.Constant)
                and allow_nan.value is True
                and has_bounds
            ):
                errors.append(
                    "st.floats() uses allow_nan=True together with "
                    "min_value/max_value"
                )

    errors.extend(_check_valid_leaf_purity(tree))
    errors.extend(_check_malformed_weight_wired(tree))
    errors.extend(_check_malformed_parameterised(tree))

    return errors


# ---------------------------------------------------------------------------
# Architectural checks
# ---------------------------------------------------------------------------

# String literals tomlc17 rejects. If any of these is reachable from the
# valid leaf, every nested array and inline table can contain a value the
# parser refuses — which is the failure mode that has survived every
# prose-based attempt to prevent it.
_PARSER_INVALID_LITERALS = {
    "1e999",
    "9223372036854775808",
    "-9223372036854775809",
}

# Names whose value feeds the recursion base. Assignments to these must not
# contain parser-invalid literals.
_VALID_LEAF_NAMES = {"st_leaf", "valid_leaf", "st_valid_leaf"}


def _literals_in(node):
    """Every string constant appearing anywhere under node."""
    out = []
    for sub in ast.walk(node):
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            out.append(sub.value)
    return out


def _check_valid_leaf_purity(tree):
    """Reject parser-invalid literals reachable from the valid leaf.

    The leaf is the base case of st.recursive, so anything invalid placed
    there contaminates every nested container. This is the single most
    important architectural rule in the seed prompt and the one most often
    violated, so it is enforced statically rather than by instruction.
    """
    errors = []

    # Names assigned a collection containing a parser-invalid literal, so
    # indirection (st_special_floats -> st_float -> st_leaf) is caught too.
    tainted = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if not isinstance(target, ast.Name):
                continue
            bad = [
                lit for lit in _literals_in(node.value)
                if lit.strip() in _PARSER_INVALID_LITERALS
            ]
            if bad:
                tainted[target.id] = sorted(set(bad))

    if not tainted:
        return errors

    # Propagate taint through assignments that reference a tainted name.
    for _ in range(5):
        grew = False
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            for target in node.targets:
                if not isinstance(target, ast.Name) or target.id in tainted:
                    continue
                refs = {
                    s.id for s in ast.walk(node.value)
                    if isinstance(s, ast.Name)
                }
                hit = refs & set(tainted)
                if hit:
                    tainted[target.id] = sorted(
                        {v for k in hit for v in tainted[k]}
                    )
                    grew = True
        if not grew:
            break

    for leaf_name in _VALID_LEAF_NAMES:
        if leaf_name in tainted:
            errors.append(
                f"parser-invalid literal(s) {tainted[leaf_name]} are "
                f"reachable from `{leaf_name}`, the base case of the "
                "recursion. Every nested array and inline table can then "
                "contain a value tomlc17 rejects. These literals belong "
                "only in the malformed strategies."
            )

    return errors


def _check_malformed_weight_wired(tree):
    """Reject a MALFORMED_WEIGHT that is assigned but never read.

    A generator can declare the constant and implement its ratio some other
    way (e.g. `st.one_of([valid] * 9 + [malformed])`). The knob is then
    decorative: the loop writes a new value every iteration and nothing
    changes, and the valid path cannot be isolated for measurement.
    """
    assigned = read = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == "MALFORMED_WEIGHT":
            if isinstance(node.ctx, ast.Store):
                assigned = True
            elif isinstance(node.ctx, ast.Load):
                read = True

    if assigned and not read:
        return [
            "MALFORMED_WEIGHT is assigned but never read. The valid/malformed "
            "ratio must be controlled BY that constant — draw a value and "
            "compare it against MALFORMED_WEIGHT. Do not encode the ratio by "
            "repeating a branch inside st.one_of(); Hypothesis does not "
            "sample branch repetition in the requested proportion, and the "
            "constant then controls nothing."
        ]
    if not assigned:
        return ["MALFORMED_WEIGHT is not defined."]
    return []


def _check_malformed_parameterised(tree):
    """Reject malformed strategies that are mostly fixed string constants.

    `st.just("x = TRUE")` emits one identical input for the entire campaign.
    A malformed channel built from constants is a small fixed corpus, not a
    strategy: it cannot vary its surrounding structure, so it explores one
    point rather than a region.
    """
    just_only = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if not isinstance(target, ast.Name):
                continue
            if not target.id.startswith("_malformed_"):
                continue
            call = node.value
            if (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and call.func.attr == "just"
            ):
                just_only.append(target.id)

    total = sum(
        1 for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        for t in node.targets
        if isinstance(t, ast.Name) and t.id.startswith("_malformed_")
    )

    if total and len(just_only) > total / 2:
        return [
            f"{len(just_only)} of {total} malformed strategies are bare "
            f"st.just(...) constants ({', '.join(sorted(just_only)[:5])}"
            f"{'...' if len(just_only) > 5 else ''}). Each emits one "
            "identical input for the whole campaign. Parameterise them: draw "
            "the key and the surrounding document, and make the malformed "
            "element the single deliberate change."
        ]
    return []


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------
def generate_strategy(
    version=1,
    feedback=None,
    campaign_signals=None,
    malformed_feedback=None,
    structural_feedback=None,
    capability_feedback=None,
    malformed_weight=None,
    max_depth=None,
    verbose=True,
    base_version=None,
    ):
    """Generate a Hypothesis TOML generator via Gemini.

    Args:
        version: iteration number. 1 = seed, 2+ = refinement.
        feedback: V1 only — diagnosis from previous failed seed attempts.
        campaign_signals: list[str] or str — instructions from feedback.py.
        malformed_feedback: str — from malformed_tracker.build_instruction().
        structural_feedback: str — from structural_metrics.build_instruction().
        capability_feedback: str — from capability_check.build_instruction().
        malformed_weight: float — MALFORMED_WEIGHT for this iteration.

    Returns:
        (output_file: Path, result: dict). result carries "usage",
        "interaction_id", plus "prompt_chars" and "sections_sent".
    """
    if not SEED_PROMPT_FILE.exists():
        raise FileNotFoundError(f"Seed prompt not found: {SEED_PROMPT_FILE}")

    seed_prompt = SEED_PROMPT_FILE.read_text(encoding="utf-8")
    prompt_parts = [seed_prompt]
    sections_sent = []

    # -----------------------------------------------------------------
    # V1 — seed generation
    # -----------------------------------------------------------------
    if version == 1:
        prompt_parts.append("""

--- SEED GENERATION TASK ---

Generate the initial TOML fuzzing generator from the specification above.

This is the first iteration: there is no previous generator and no campaign
feedback. Use the grammar and the structural skeleton to build the strongest
broad-coverage seed generator you can.

Fill every <FILL> section. Copy the fixed harness section verbatim.

Use the Hypothesis version installed in the project environment.
For floating-point strategies, use `allow_infinity`, not `allow_inf`.

Hypothesis strategies must be deterministic and stable. Do not make strategy
generation depend on mutable global state, counters, random module state,
time, filesystem state, environment state, or side effects. Do not mutate
shared objects while generating examples. Repeated calls to
toml_documents.example() must be able to generate independently and
consistently. Avoid custom strategies or @st.composite functions that depend
on external state.

The generated module must be directly executable and importable, and must
define a Hypothesis SearchStrategy named `toml_documents`.
""")

        if feedback:
            sections_sent.append("v1_retry_diagnosis")
            prompt_parts.append("""
--- PREVIOUS ATTEMPT(S) FAILED ---

Earlier attempts at this seed generator failed validation. The diagnosis
below reports the exact failure. Correct ONLY the reported failure.

MINIMAL-REPAIR RULE:
- Preserve all working parts of the previous generator.
- Do not redesign or rewrite the generator architecture.
- Do not remove capabilities to make validation pass.
- Do not introduce new Hypothesis APIs unless they are known to exist in
  the installed project environment.
- Make the smallest possible change that fixes the reported error.
- After fixing the reported error, preserve the required contract:
  `toml_documents` must remain a Hypothesis SearchStrategy.

""" + _block("PREVIOUS_ATTEMPT_DIAGNOSIS", feedback) + """
The generated module must preserve this exact contract:
- toml_documents must be a Hypothesis SearchStrategy object.
- Do not redefine toml_documents as a function, list, string, or callable.
- Functions that return a SearchStrategy must return the strategy object itself.
  Do not write `return strategy()` when `strategy` is already a SearchStrategy.
- Do not change working architecture while fixing the reported error.
- Fix only the diagnosed problem and preserve all existing capabilities.

Use only Hypothesis APIs that exist in the installed project environment.

Known compatibility rule:
- Do not use st.zip(). To combine strategies, use
  st.tuples(...).map(...) or another supported Hypothesis composition.

If the diagnosis reports an unsupported API, replace only that API with
the supported equivalent. Do not redesign the generator.

Do not respond by narrowing the generator. Raising the acceptance rate by
removing nesting, escapes, datetimes, or unicode is a regression, not a fix.
Keep the structural coverage and correct the strategies emitting values
tomlc17 rejects.
""")

        prompt_parts.append("""
Return ONLY the complete Python source code for the generator.
Do not include Markdown fences, explanations, or commentary.
""")

    # -----------------------------------------------------------------
    # V2+ — refinement
    # -----------------------------------------------------------------
    else:
        previous_version = (
        base_version if base_version is not None else version - 1
    )
        previous_file = OUTPUT_DIR / f"generator_v{previous_version}.py"
        if not previous_file.exists():
            raise FileNotFoundError(
                f"Previous generator not found: {previous_file}"
            )
        current_generator = previous_file.read_text(encoding="utf-8")

        sections_text, present = build_refinement_sections(
            campaign_signals=campaign_signals,
            malformed_feedback=malformed_feedback,
            structural_feedback=structural_feedback,
            capability_feedback=capability_feedback,
            malformed_weight=malformed_weight,
            max_depth=max_depth,
        )

        if not present:
            raise ValueError(
                "Refinement iterations require at least one feedback section."
            )
        sections_sent.extend(present)

        prompt_parts.append("""

--- CURRENT GENERATOR ---

Below is the generator produced by the previous iteration. Treat it as the
implementation to refine, and the seed prompt above as the stable
specification it must continue to satisfy.
""")
        prompt_parts.append(_block("CURRENT_GENERATOR", current_generator))
        prompt_parts.append(_SECTION_GUIDE)
        prompt_parts.append(sections_text)
        prompt_parts.append(_REFINEMENT_RULES)

    prompt = "".join(prompt_parts)
    OUTPUT_DIR.mkdir(exist_ok=True)

    if verbose:
        approx_tokens = len(prompt) // CHARS_PER_TOKEN
        print(
            f"  prompt: {len(prompt):,} chars (~{approx_tokens:,} tokens)  "
            f"sections: {sections_sent or ['seed']}",
            flush=True,
        )

    client = GeminiClient()
    result = client.generate(prompt)

    code = _strip_fences(result["text"])

    # Static safety gate
    static_errors = validate_generated_source(code)

    if static_errors:
        raise ValueError(
            "Generated strategy failed static safety checks:\n- "
            + "\n- ".join(static_errors)
        )

    output_file = OUTPUT_DIR / f"generator_v{version}.py"
    output_file.write_text(code, encoding="utf-8")

    _validate_module(output_file, version)

    result["prompt_chars"] = len(prompt)
    result["sections_sent"] = sections_sent

    if verbose:
        print(f"  generated and validated: {output_file}")
        print(f"  usage: {result.get('usage')}")

    return output_file, result


if __name__ == "__main__":
    # Show the assembled refinement sections without calling the API.
    sections, present = build_refinement_sections(
        campaign_signals=[
            "SIGNAL: generate more valid TOML around newly reached structures.",
            "SIGNAL: explore the DUPLICATE_KEY rejection boundary.",
        ],
        malformed_feedback=(
            "MALFORMED EXPLORATION (evidence from previous campaigns):\n"
            "26 of 32 categories have never been reached. Priority targets:\n"
            "  - ARRAY_PARSE\n  - BAD_ESCAPE\n  - UNTERMINATED_STRING"
        ),
        structural_feedback=(
            "STRUCTURAL REACH (measured on generated inputs):\n"
            "  nesting depth max 11, container size max 13"
        ),
        capability_feedback=(
            "MISSING MALFORMED STRATEGIES: _malformed_deep_nesting, "
            "_malformed_large_container. Reinstate them."
        ),
        malformed_weight=0.15,
    )

    print("sections present:", present)
    print(sections)
    print(f"sections add {len(sections):,} chars "
          f"(~{len(sections)//CHARS_PER_TOKEN:,} tokens) to the prompt")
