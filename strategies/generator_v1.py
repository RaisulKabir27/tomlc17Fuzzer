from hypothesis import strategies as st

# --- feedback-controlled knobs. The refinement loop rewrites these lines. ---
MALFORMED_WEIGHT = 0.15   # share of documents routed to the malformed branch
MAX_DEPTH = 8             # ceiling on drawn container nesting depth
# ---------------------------------------------------------------------------

def _bare_name():
    return st.text(alphabet="abcdefghijklmnopqrstuvwxyz",
                   min_size=1, max_size=6)

def _unique_names(count, prefix):
    return st.lists(_bare_name(), min_size=count, max_size=count).map(
        lambda stems: [f"{prefix}{i}_{s}" for i, s in enumerate(stems)]
    )

@st.composite
def _nest(draw, inner_strategy, max_depth=None):
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

# ---- FILL 1: VALID LEAF values ----
st_leaf = st.one_of(
    st.booleans().map(lambda b: "true" if b else "false"),
    st.integers(min_value=-2**63, max_value=2**63 - 1).map(str),
    st.floats(min_value=-1e308, max_value=1e308, allow_infinity=False, allow_nan=False).map(str),
    st.sampled_from(["inf", "+inf", "-inf", "nan", "+nan", "-nan"]),
    st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789", min_size=1, max_size=10).map(lambda s: f'"{s}"'),
    st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789", min_size=1, max_size=10).map(lambda s: f"'{s}'"),
    st.just("2023-10-27T10:00:00Z"),
    st.just("2023-10-27T10:00:00"),
    st.just("2023-10-27"),
    st.just("10:00:00")
)

# ---- FILL 2: VALID VALUE ----
def st_value():
    return st.recursive(
        st_leaf,
        lambda children: st.one_of(
            children,
            st.lists(children, min_size=0, max_size=3).map(lambda l: "[" + ", ".join(l) + "]"),
            st.lists(st.tuples(_bare_name(), children), min_size=0, max_size=3).map(
                lambda l: "{ " + ", ".join([f"{k} = {v}" for k, v in l]) + " }"
            ),
            _nest(children)
        ),
        max_leaves=5
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
        if "." in k or draw(st.booleans()):
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
    p = draw(_bare_name())
    return f"{p}.a = {draw(st_value())}\n{p}.a = {draw(st_value())}"

@st.composite
def _malformed_extreme_number(draw):
    return f"x = {draw(st.one_of(st.just('9223372036854775808'), st.just('1e999')))}"

@st.composite
def _malformed_control_char(draw):
    return 'x = "\\u0000"'

@st.composite
def _malformed_table_value_conflict(draw):
    k = draw(_bare_name())
    return f"{k} = {draw(st_value())}\n[{k}]"

def _malformed_document():
    return st.one_of(
        _malformed_duplicate_key(),
        _malformed_dotted_key_conflict(),
        _malformed_extreme_number(),
        _malformed_control_char(),
        _malformed_table_value_conflict()
    )

toml_documents = _toml_documents()

if __name__ == "__main__":
    for _ in range(10):
        print(toml_documents.example())
        print("-" * 20)