from hypothesis import strategies as st

# ============================================================================
# FIXED PLUMBING — COPY THIS ENTIRE BLOCK VERBATIM. DO NOT REDESIGN IT.
# It contains no TOML content decisions; it only makes depth, size and
# uniqueness reachable and safe.
# ============================================================================
from hypothesis import strategies as st

# --- feedback-controlled knobs. The refinement loop rewrites these lines. ---
MALFORMED_WEIGHT = 0.20   # share of documents routed to the malformed branch
MAX_DEPTH = 32             # ceiling on drawn container nesting depth
# ---------------------------------------------------------------------------

def _bare_name():
    return st.text(alphabet="abcdefghijklmnopqrstuvwxyz",
                   min_size=1, max_size=6)

def _unique_names(count, prefix):
    """A STRATEGY producing `count` distinct TOML bare names.

    Call as:  names = draw(_unique_names(5, "k"))
    It RETURNS a strategy, so pass it to draw(). Never pass draw into it.
    Independently drawn keys collide constantly, and one collision makes the
    whole document invalid at that line, so nothing after it is parsed.
    """
    return st.lists(_bare_name(), min_size=count, max_size=count).map(
        lambda stems: [f"{prefix}{i}_{s}" for i, s in enumerate(stems)]
    )

@st.composite
def _nest(draw, inner_strategy, max_depth=None):
    """Wrap a value in a DRAWN number of container levels, 1..MAX_DEPTH.

    st.recursive with max_leaves cannot control depth: Hypothesis biases
    recursion shallow, so observed depth stays near the low end whatever the
    leaf budget. Drawing the depth makes it a property the feedback loop can
    actually move by rewriting MAX_DEPTH.
    """
    limit = MAX_DEPTH if max_depth is None else max_depth
    depth = draw(st.integers(min_value=1, max_value=max(1, limit)))
    text = draw(inner_strategy)
    for _ in range(depth):
        if draw(st.booleans()):
            text = f"[{text}]"
        else:
            text = f"{{ {draw(_bare_name())} = {text} }}"
    return text

@st.composite
def _toml_documents(draw):
    if draw(st.floats(min_value=0.0, max_value=1.0)) < MALFORMED_WEIGHT:
        return draw(_malformed_document())
    return draw(valid_document())
# ============================================================================
# END FIXED PLUMBING
# ============================================================================

# ---- FILL 1: VALID LEAF values ----
st_leaf = st.one_of(
    st.booleans().map(lambda b: "true" if b else "false"),
    st.integers(min_value=-9223372036854775808, max_value=9223372036854775807).map(str),
    st.floats(allow_nan=False, allow_infinity=False).map(str),
    st.sampled_from(["inf", "-inf", "+inf", "nan", "-nan", "+nan"]),
    st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789", min_size=1, max_size=10).map(lambda s: f'"{s}"'),
    st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789", min_size=1, max_size=10).map(lambda s: f"'{s}'"),
    st.text(alphabet="abcdefghijklmnopqrstuvwxyz", min_size=1, max_size=5).map(lambda s: f'"""{s}"""'),
    st.just("2023-10-27T10:00:00Z"),
    st.just("2023-10-27T14:15:00"),
    st.just("2023-10-27"),
    st.just("14:15:00")
)

# ---- FILL 2: VALID VALUE ----
def st_value():
    return st.recursive(
        st_leaf,
        lambda children: st.one_of(
            children,
            st.lists(children, min_size=0, max_size=5).map(lambda l: "[" + ", ".join(l) + "]"),
            st.lists(st.tuples(_bare_name(), children), min_size=0, max_size=5).map(
                lambda l: "{ " + ", ".join([f"{k} = {v}" for k, v in l]) + " }"
            ),
            _nest(children)
        ),
        max_leaves=10
    )

# ---- FILL 3: VALID DOCUMENT ----
@st.composite
def valid_document(draw):
    n_keys = draw(st.integers(min_value=1, max_value=8))
    keys = draw(_unique_names(n_keys, "k"))
    n_tables = draw(st.integers(min_value=0, max_value=3))
    table_names = draw(_unique_names(n_tables, "t"))
    
    doc = []
    for k in keys:
        if draw(st.booleans()):
            k = f"{k}.{draw(_bare_name())}"
        doc.append(f"{k} = {draw(st_value())}")
        
    for t in table_names:
        doc.append(f"[{t}]")
        for _ in range(draw(st.integers(min_value=0, max_value=2))):
            doc.append(f"{draw(_bare_name())} = {draw(st_value())}")
            
    return "\n".join(doc)

# ---- FILL 4: MALFORMED strategies ----
@st.composite
def _malformed_duplicate_key(draw):
    k = draw(_bare_name())
    return f"{k} = {draw(st_value())}\n{k} = {draw(st_value())}"

@st.composite
def _malformed_dotted_key_conflict(draw):
    k = draw(_bare_name())
    return f"{k}.a = {draw(st_value())}\n{k}.a = {draw(st_value())}"

@st.composite
def _malformed_table_conflict(draw):
    k = draw(_bare_name())
    return f"[{k}]\n{k} = 1"

@st.composite
def _malformed_table_value_conflict(draw):
    k = draw(_bare_name())
    return f"{k} = {draw(st_value())}\n[{k}]"

@st.composite
def _malformed_int_overflow(draw):
    return "x = 9223372036854775808"

@st.composite
def _malformed_float_overflow(draw):
    return "x = 1e999"

@st.composite
def _malformed_array_parse(draw):
    return "x = [1,,2]"

@st.composite
def _malformed_bad_escape(draw):
    return r'x = "\z"'

@st.composite
def _malformed_bad_hex_escape(draw):
    return r'x = "\xZZ"'

@st.composite
def _malformed_inline_table_ext(draw):
    k = draw(_bare_name())
    return f"{k} = {{ a = 1 }}\n{k}.a = 2"

@st.composite
def _malformed_invalid_boolean(draw):
    return "x = tru"

@st.composite
def _malformed_invalid_datetime(draw):
    return "x = 2023-10-27T10:00:00Z9"

@st.composite
def _malformed_invalid_string_char(draw):
    return r'x = "\x00"'

@st.composite
def _malformed_invalid_value(draw):
    return "x = ."

@st.composite
def _malformed_expect_equals(draw):
    return "x y = 1"

def _malformed_document():
    return st.one_of(
        _malformed_duplicate_key(),
        _malformed_dotted_key_conflict(),
        _malformed_table_conflict(),
        _malformed_table_value_conflict(),
        _malformed_int_overflow(),
        _malformed_float_overflow(),
        _malformed_array_parse(),
        _malformed_bad_escape(),
        _malformed_bad_hex_escape(),
        _malformed_inline_table_ext(),
        _malformed_invalid_boolean(),
        _malformed_invalid_datetime(),
        _malformed_invalid_string_char(),
        _malformed_invalid_value(),
        _malformed_expect_equals()
    )

toml_documents = _toml_documents()

if __name__ == "__main__":
    for _ in range(10):
        print(toml_documents.example())
        print("-" * 20)