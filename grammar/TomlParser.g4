parser grammar TomlParser;

options {
    tokenVocab = TomlLexer;
}

document
    : expression (NL expression)* EOF
    ;

expression
    : key_value comment
    | table comment
    | comment
    ;

comment
    : COMMENT?
    ;

key_value
    : key EQUALS value
    ;

key
    : simple_key
    | dotted_key
    ;

simple_key
    : quoted_key
    | unquoted_key
    ;

unquoted_key
    : UNQUOTED_KEY
    ;

quoted_key
    : BASIC_STRING
    | LITERAL_STRING
    ;

dotted_key
    : simple_key (DOT simple_key)+
    ;

value
    : string
    | integer
    | floating_point
    | bool_
    | date_time
    | array_
    | inline_table
    ;

string
    : BASIC_STRING
    | ML_BASIC_STRING
    | LITERAL_STRING
    | ML_LITERAL_STRING
    ;

integer
    : DEC_INT
    | HEX_INT
    | OCT_INT
    | BIN_INT
    ;

floating_point
    : FLOAT
    | INF
    | NAN
    ;

bool_
    : BOOLEAN
    ;

date_time
    : OFFSET_DATE_TIME
    | LOCAL_DATE_TIME
    | LOCAL_DATE
    | LOCAL_TIME
    ;

array_
    : L_BRACKET array_values? comment_or_nl R_BRACKET
    ;

array_values
    : comment_or_nl value nl_or_comment COMMA array_values comment_or_nl
    | comment_or_nl value nl_or_comment COMMA?
    ;

comment_or_nl
    : (COMMENT? NL)*
    ;

nl_or_comment
    : (NL COMMENT?)*
    ;

table
    : standard_table
    | array_table
    ;

standard_table
    : L_BRACKET key R_BRACKET
    ;

array_table
    : DOUBLE_L_BRACKET key DOUBLE_R_BRACKET
    ;

/*
 * TOML 1.1 / tomlc17:
 * inline tables may contain newlines/comments and may end with a trailing comma.
 */
inline_table
    : L_BRACE inline_table_ws inline_table_keyvals? inline_table_ws R_BRACE
    ;

inline_table_keyvals
    : inline_table_keyval
      (inline_table_ws COMMA inline_table_ws inline_table_keyval)*
      inline_table_ws COMMA?
    ;

inline_table_keyval
    : key EQUALS value
    ;

inline_table_ws
    : (NL | COMMENT)*
    ;
