# Adapted tomlc17 Grammar — Fixes Applied

Target: **tomlc17**, commit `75565ea` (release `R260618`), which implements
**TOML 1.1**. Every fix below is grounded in tomlc17's own source, so the grammar
describes what the *implementation* accepts, not merely what a spec says.

This document covers the fixable limitations. It deliberately excludes the
**Group C implementation limits** (nesting depth 30, 16 384 element cap, int64
range, float range, 10-part dotted-key cap): those are intentionally NOT encoded
in the grammar, because baking a runtime limit into the grammar would stop the
generator from ever producing the boundary-crossing inputs where bugs live. They
are handled as generator targets and documented separately.

---

## Group A — fixed in the grammar (`.g4`)

### FIX 1 — Mode-stack correction for nested containers (was limitation #11)

**Symptom (before the fix):** a nested inline table such as
`a = { b = { c = 1 } }` produces a token-recognition error on the second `}`.

**Root cause:** `SIMPLE_VALUE_MODE` is a *one-shot* lexer mode: `=` pushes it and
it is meant to hold exactly one value. The previous version entered container
values with `pushMode(...)`, which stacks the container mode *on top of* the
one-shot mode. Because a container value never triggers the scalar `popMode`, the
one-shot `SIMPLE_VALUE_MODE` is left stranded on the mode stack; every nesting
level strands another, so the stack drifts and the closing brace arrives in the
wrong mode.

**Fix:** enter container values with `mode(...)` instead of `pushMode(...)`, in
`SIMPLE_VALUE_MODE` only:
```
L_BRACE     : '{'       -> mode(INLINE_TABLE_MODE);          // was pushMode
ARRAY_START : L_BRACKET -> type(L_BRACKET), mode(ARRAY_MODE); // was pushMode
```
`mode()` *replaces* the one-shot value mode (consuming the value slot) rather than
stacking on it. The container's own closer (`R_BRACE` / `ARRAY_END`) then pops the
context that the enclosing `=` saved, so the stack stays balanced at any depth.

Note the contrast: inside `INLINE_TABLE_MODE` and `ARRAY_MODE`, opening a nested
container still correctly uses `pushMode(...)` — there we are adding a genuine
nesting level, not replacing a one-shot value mode.

**Verify with:** `valid/03_nested_inline_tables.toml`,
`valid/02_nested_arrays.toml`, `valid/04_array_of_inline_tables.toml`.

---

### FIX 2 — Unicode bare keys (was limitation #1)

**Before:** `UNQUOTED_KEY` allowed only `[A-Za-z0-9_-]`, so a TOML 1.1 Unicode
bare key (e.g. `α = 1`) was rejected though tomlc17 accepts it.

**Fix:** added a `BARE_KEY_CHAR` fragment whose ranges are copied **verbatim from
tomlc17's `is_unicode_bare_key_char()`** (tomlc17.c). Using the implementation's
own ranges rather than re-deriving from the spec means the grammar matches
tomlc17 exactly. Ranges: `0xB2 0xB3 0xB9`, `0xBC–0xBE`, `0xC0–0xD6`, `0xD8–0xF6`,
`0xF8–0x37D`, `0x37F–0x1FFF`, `0x200C 0x200D`, `0x203F–0x2040`, `0x2070–0x218F`,
`0x2460–0x24FF`, `0x2C00–0x2FEF`, `0x3001–0xD7FF`, `0xF900–0xFDCF`,
`0xFDF0–0xFFFD`, `0x10000–0xEFFFF`.

**Verify with:** `valid/10_unicode_bare_key.toml` (uses U+03B1 α).

---

### FIX 3 — TOML 1.1 escape sequences (part of the earlier adaptation)

Removed `\/` from the `ESC` fragment (TOML 1.0 removed forward slash as an
escapable character, so the grammar must reject it) and added the two TOML 1.1
escapes `\e` and `\xHH` (`'e'` and `'x' HEX_DIGIT HEX_DIGIT`).

**Verify with:** `valid/06_new_escapes.toml` (accept `\e`, `\xHH`) and
`invalid/01_forward_slash_escape.toml` (reject `\/`).

---

### FIX 4 — Control-character restrictions across all string types (was #8)

**Before:** only `BASIC_STRING` excluded control characters; `LITERAL_STRING` and
the multi-line strings allowed them.

**Fix:** applied a consistent control-char exclusion to `BASIC_STRING`,
`LITERAL_STRING`, `ML_BASIC_STRING`, and `ML_LITERAL_STRING`, matching tomlc17's
`is_valid_char()` (allows `0x20–0x7E`, high-bit `0x80+`, and tab `0x09`; rejects
`0x00–0x08`, `0x0A–0x1F`, `0x7F`). Single-line strings additionally forbid raw
CR/LF; multi-line strings permit tab and newline. Excluded set used:
`\u0000-\u0008 \u000B \u000C \u000E-\u001F \u007F` (note `\u0009` tab is allowed).

**Verify with:** `invalid/04_control_char_in_string.toml`.

---

### FIX 5 — Multi-line inline tables + trailing commas (part of the earlier adaptation)

`INLINE_TABLE_MODE` now emits `NL`/`COMMENT` (via `INLINE_TABLE_NL` /
`INLINE_TABLE_COMMENT`), and the parser's `inline_table` rules allow interleaved
newlines/comments and an optional trailing comma — the TOML 1.1 relaxation.

**Verify with:** `valid/05_multiline_inline_table_trailing_comma.toml`.

---

## Group B — cannot be fixed in a context-free grammar (documented, not encoded)

These depend on document-level state, which is beyond context-free power. The
grammar therefore (correctly) accepts them syntactically; they are enforced by
tomlc17 semantically. They are handled as **adaptation notes that seed the LLM**,
and they double as valuable near-valid-but-invalid test inputs.

- **Duplicate keys (was #2):** `x = 1` / `x = 2` is syntactically generable; a CFG
  cannot express "this key was not already defined." tomlc17 rejects it
  (`tab_emplace` uses non-zero type to detect duplicates).
- **Table / dotted-key conflicts (was #9):** redefining a value as a table,
  conflicting `[t]` vs `[[t]]`, extending an inline table — all document-state
  dependent, all beyond a CFG.

**Treatment:** documented in the adaptation notes; the generator is instructed to
emit these deliberately so we can test *how* tomlc17 rejects them (clean error vs.
crash).

---

## Not encoded in the grammar by choice (was #7)

**Unicode surrogate / scalar-value restriction:** `\uD800`–`\uDFFF` are invalid
scalar values. Expressing "these four hex digits must not fall in this numeric
range" in a lexer would require enumerating hex combinations — ugly and
error-prone. **Decision:** keep it out of the grammar and handle it generator-side
(never emit a surrogate when producing a *valid* string; emit one deliberately as
an *invalid* case). This is a documented judgment call, not an oversight.

---

## Status / caveat

This grammar has **not yet been compiled** — it must be generated with ANTLR and
run against the test corpus (`../testrig`) to confirm it is valid ANTLR and that
every `valid/` case is accepted and every `invalid/` case rejected. FIX 1 (the
mode stack) is the highest-risk change and should be validated first with the
nested-container cases.
