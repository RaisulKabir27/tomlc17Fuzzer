
set -e

TOMLC17_SRC="${TOMLC17_SRC:-./tomlc17-source/src}"
# Your clang (MSYS2 clang64):
CLANG="${CLANG:-/c/msys64/clang64/bin/clang}"

if [ ! -f "$TOMLC17_SRC/tomlc17.c" ]; then
    echo "ERROR: $TOMLC17_SRC/tomlc17.c not found."
    echo "Set TOMLC17_SRC to the folder holding tomlc17.c and tomlc17.h, e.g.:"
    echo "  TOMLC17_SRC=/d/path/to/tomlc17/src bash build_toml.sh"
    exit 1
fi

mkdir -p harness

"$CLANG" -g -O1 \
  -fsanitize=address,undefined \
  -fsanitize-ignorelist="$PWD/ubsan.ignore" \
  -fno-omit-frame-pointer \
  -I "$TOMLC17_SRC" \
  harness/toml_harness.c "$TOMLC17_SRC/tomlc17.c" \
  -o harness/toml_harness.exe
