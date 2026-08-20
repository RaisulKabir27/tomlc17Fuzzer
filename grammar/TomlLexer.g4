lexer grammar TomlLexer;

// =============================================================================
// Adapted TOML 1.1 lexer for tomlc17 (commit 75565ea, release R260618).
//
// Base: grammars-v4 TOML grammar.
// Changes from the previous adapted version are marked with  [FIX n]  and are
// documented in FIXES.md. Ranges for Unicode bare keys and string control-char
// restrictions are taken from tomlc17's own source (is_unicode_bare_key_char()
// and is_valid_char()), so this describes what tomlc17 ACTUALLY accepts.
// =============================================================================

WS : [ \t]+ -> skip;
NL : ('\r'? '\n')+;
COMMENT : '#' ~[\r\n]*;

L_BRACKET : '[';
DOUBLE_L_BRACKET : '[[';
R_BRACKET : ']';
DOUBLE_R_BRACKET : ']]';
EQUALS : '=' -> pushMode(SIMPLE_VALUE_MODE);
DOT : '.';
COMMA : ',';

fragment DIGIT : [0-9];
fragment ALPHA : [A-Za-z];

// [FIX 3] TOML 1.1 escapes: '\e' and '\xHH' added; forward slash '\/' removed
// (TOML 1.0 removed '/' as an escapable char, so the grammar must not allow it).
fragment ESC
    : '\\' (["\\bfnrt] | 'e' | 'x' HEX_DIGIT HEX_DIGIT | UNICODE | EX_UNICODE)
    ;

fragment UNICODE
    : 'u' HEX_DIGIT HEX_DIGIT HEX_DIGIT HEX_DIGIT
    ;

fragment EX_UNICODE
    : 'U' HEX_DIGIT HEX_DIGIT HEX_DIGIT HEX_DIGIT
      HEX_DIGIT HEX_DIGIT HEX_DIGIT HEX_DIGIT
    ;

// [FIX 4] Control-character restrictions.
// tomlc17's is_valid_char() allows 0x20-0x7E, high-bit (0x80+), plus tab (0x09).
// It rejects 0x00-0x08, 0x0A-0x1F, and 0x7F inside single-line strings.
// Basic strings additionally forbid raw CR/LF and the backslash/quote.
BASIC_STRING
    : '"' (ESC | ~["\\\r\n\u0000-\u0008\u000B\u000C\u000E-\u001F\u007F])*? '"'
    ;

// [FIX 4] Literal strings: no escaping, but same control-char rejection and no
// raw newline. Previously this allowed any control char except quote/CR/LF.
LITERAL_STRING
    : '\'' ~['\r\n\u0000-\u0008\u000B\u000C\u000E-\u001F\u007F]*? '\''
    ;

// [FIX 2] Unicode bare keys (TOML 1.1). Ranges copied from tomlc17's
// is_unicode_bare_key_char(). Previously only [A-Za-z0-9_-] was allowed.
fragment BARE_KEY_CHAR
    : [A-Za-z0-9_-]
    | [\u00B2\u00B3\u00B9\u00BC-\u00BE\u00C0-\u00D6\u00D8-\u00F6\u00F8-\u037D\u037F-\u1FFF]
    | [\u200C\u200D\u203F\u2040\u2070-\u218F\u2460-\u24FF\u2C00-\u2FEF\u3001-\uD7FF]
    | [\uF900-\uFDCF\uFDF0-\uFFFD]
    | [\u{10000}-\u{EFFFF}]
    ;

UNQUOTED_KEY
    : BARE_KEY_CHAR+
    ;

mode SIMPLE_VALUE_MODE;

VALUE_WS : WS -> skip;

// [FIX 1] Reverted from pushMode() to mode(). SIMPLE_VALUE_MODE is a one-shot
// mode pushed by '='. A container value must REPLACE it (mode()), not stack on
// top of it (pushMode()); otherwise the one-shot mode is left stranded on the
// mode stack and nesting drifts. The container's own closer pops the context
// saved by the enclosing '='. This makes nested inline tables / arrays balance.
L_BRACE
    : '{' -> mode(INLINE_TABLE_MODE)
    ;

// [FIX 1] Same revert for arrays: mode() not pushMode().
ARRAY_START
    : L_BRACKET -> type(L_BRACKET), mode(ARRAY_MODE)
    ;

BOOLEAN
    : ('true' | 'false') -> popMode
    ;

fragment ML_ESC
    : '\\' '\r'? '\n'
    | ESC
    ;

VALUE_BASIC_STRING
    : BASIC_STRING -> type(BASIC_STRING), popMode
    ;

// [FIX 4] Multi-line basic string: allow tab/newline, reject other control chars.
ML_BASIC_STRING
    : '"""' (ML_ESC | ~["\\\u0000-\u0008\u000B\u000C\u000E-\u001F\u007F])*? '"""' -> popMode
    ;

VALUE_LITERAL_STRING
    : LITERAL_STRING -> type(LITERAL_STRING), popMode
    ;

// [FIX 4] Multi-line literal string: allow tab/newline, reject other control chars.
ML_LITERAL_STRING
    : '\'\'\'' (~[\u0000-\u0008\u000B\u000C\u000E-\u001F\u007F])*? '\'\'\'' -> popMode
    ;

fragment EXP
    : ('e' | 'E') [+-]? ZERO_PREFIXABLE_INT
    ;

fragment ZERO_PREFIXABLE_INT
    : DIGIT (DIGIT | '_' DIGIT)*
    ;

fragment FRAC
    : '.' ZERO_PREFIXABLE_INT
    ;

FLOAT
    : DEC_INT (EXP | FRAC EXP?) -> popMode
    ;

INF
    : [+-]? 'inf' -> popMode
    ;

NAN
    : [+-]? 'nan' -> popMode
    ;

fragment HEX_DIGIT : [A-Fa-f] | DIGIT;
fragment DIGIT_1_9 : [1-9];
fragment DIGIT_0_7 : [0-7];
fragment DIGIT_0_1 : [0-1];

DEC_INT
    : [+-]? (DIGIT | (DIGIT_1_9 (DIGIT | '_' DIGIT)+)) -> popMode
    ;

HEX_INT
    : '0x' HEX_DIGIT (HEX_DIGIT | '_' HEX_DIGIT)* -> popMode
    ;

OCT_INT
    : '0o' DIGIT_0_7 (DIGIT_0_7 | '_' DIGIT_0_7)* -> popMode
    ;

BIN_INT
    : '0b' DIGIT_0_1 (DIGIT_0_1 | '_' DIGIT_0_1)* -> popMode
    ;

fragment YEAR : DIGIT DIGIT DIGIT DIGIT;
fragment MONTH : DIGIT DIGIT;
fragment DAY : DIGIT DIGIT;
fragment DELIM : 'T' | 't' | ' ';
fragment HOUR : DIGIT DIGIT;
fragment MINUTE : DIGIT DIGIT;
fragment SECOND : DIGIT DIGIT;
fragment SECFRAC : '.' DIGIT+;
fragment NUMOFFSET : ('+' | '-') HOUR ':' MINUTE;
fragment OFFSET : 'Z' | NUMOFFSET;

// TOML 1.1: seconds may be omitted.
fragment PARTIAL_TIME
    : HOUR ':' MINUTE (':' SECOND SECFRAC?)?
    ;

fragment FULL_DATE : YEAR '-' MONTH '-' DAY;
fragment FULL_TIME : PARTIAL_TIME OFFSET;

OFFSET_DATE_TIME : FULL_DATE DELIM FULL_TIME -> popMode;
LOCAL_DATE_TIME : FULL_DATE DELIM PARTIAL_TIME -> popMode;
LOCAL_DATE : FULL_DATE -> popMode;
LOCAL_TIME : PARTIAL_TIME -> popMode;

mode INLINE_TABLE_MODE;

INLINE_TABLE_WS : [ \t]+ -> skip;
// [FIX 5] TOML 1.1: newlines/comments allowed inside inline tables (multi-line).
INLINE_TABLE_NL : NL -> type(NL);
INLINE_TABLE_COMMENT : COMMENT -> type(COMMENT);

INLINE_TABLE_KEY_DOT : DOT -> type(DOT);
INLINE_TABLE_COMMA : COMMA -> type(COMMA);

R_BRACE : '}' -> popMode;

// Nested inline table: pushMode is CORRECT here (we are already inside a
// container mode, so we add a level rather than replacing a one-shot value mode).
INLINE_TABLE_L_BRACE
    : '{' -> type(L_BRACE), pushMode(INLINE_TABLE_MODE)
    ;

INLINE_TABLE_L_BRACKET
    : '[' -> type(L_BRACKET), pushMode(ARRAY_MODE)
    ;

INLINE_TABLE_KEY_BASIC_STRING
    : BASIC_STRING -> type(BASIC_STRING)
    ;

INLINE_TABLE_KEY_LITERAL_STRING
    : LITERAL_STRING -> type(LITERAL_STRING)
    ;

INLINE_TABLE_KEY_UNQUOTED
    : UNQUOTED_KEY -> type(UNQUOTED_KEY)
    ;

INLINE_TABLE_EQUALS
    : EQUALS -> type(EQUALS), pushMode(SIMPLE_VALUE_MODE)
    ;

mode ARRAY_MODE;

ARRAY_WS : [ \t]+ -> skip;
ARRAY_NL : NL -> type(NL);
ARRAY_COMMENT : COMMENT -> type(COMMENT);
ARRAY_COMMA : COMMA -> type(COMMA);

// Nested container inside an array: pushMode is CORRECT (adding a level).
ARRAY_INLINE_TABLE_START
    : '{' -> type(L_BRACE), pushMode(INLINE_TABLE_MODE)
    ;

NESTED_ARRAY_START
    : '[' -> type(L_BRACKET), pushMode(ARRAY_MODE)
    ;

ARRAY_END
    : ']' -> type(R_BRACKET), popMode
    ;

ARRAY_BOOLEAN : BOOLEAN -> type(BOOLEAN);
ARRAY_BASIC_STRING : BASIC_STRING -> type(BASIC_STRING);
ARRAY_ML_BASIC_STRING : ML_BASIC_STRING -> type(ML_BASIC_STRING);
ARRAY_LITERAL_STRING : LITERAL_STRING -> type(LITERAL_STRING);
ARRAY_ML_LITERAL_STRING : ML_LITERAL_STRING -> type(ML_LITERAL_STRING);

ARRAY_FLOAT : FLOAT -> type(FLOAT);
ARRAY_INF : INF -> type(INF);
ARRAY_NAN : NAN -> type(NAN);

ARRAY_DEC_INT : DEC_INT -> type(DEC_INT);
ARRAY_HEX_INT : HEX_INT -> type(HEX_INT);
ARRAY_OCT_INT : OCT_INT -> type(OCT_INT);
ARRAY_BIN_INT : BIN_INT -> type(BIN_INT);

ARRAY_OFFSET_DATE_TIME : OFFSET_DATE_TIME -> type(OFFSET_DATE_TIME);
ARRAY_LOCAL_DATE_TIME : LOCAL_DATE_TIME -> type(LOCAL_DATE_TIME);
ARRAY_LOCAL_DATE : LOCAL_DATE -> type(LOCAL_DATE);
ARRAY_LOCAL_TIME : LOCAL_TIME -> type(LOCAL_TIME);
