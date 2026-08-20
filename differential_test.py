#!/usr/bin/env python3
from __future__ import annotations
import json
import subprocess
import sys
from pathlib import Path
from runner import run_harness

ROOT = Path(__file__).resolve().parent
ANTLR_DIR = ROOT / "antlr-test"
TEST_ROOT = ANTLR_DIR / "tests"
SEMANTIC_DIR = ROOT / "tomlc17_differential_validation" / "semantic_invalid"
RESULT_FILE = ROOT / "differential_results.json"

def antlr_result(path):
    p = subprocess.run(
        [sys.executable, "driver.py", str(path)],
        cwd=ANTLR_DIR, capture_output=True, text=True
    )
    if p.returncode == 0: return "ACCEPT", p.stderr.strip()
    if p.returncode == 1: return "REJECT", p.stderr.strip()
    return "RUNTIME_ERROR", p.stderr.strip()

def tomlc17_result(path):
    r = run_harness(str(path))
    s = r.get("status", "UNKNOWN")
    if s == "VALID": return "ACCEPT", r
    if s == "REJECT": return "REJECT", r
    return s, r

def group(name, files, expected_a=None, expected_t=None):
    rows = []
    print(f"\n=== {name} ===")
    for path in sorted(files):
        a, ad = antlr_result(path)
        t, td = tomlc17_result(path)
        if expected_a and a != expected_a:
            v = "GRAMMAR_MISMATCH"
        elif expected_t and t != expected_t:
            v = "TOMLC17_UNEXPECTED"
        elif a == "RUNTIME_ERROR":
            v = "ANTLR_RUNTIME_ERROR"
        elif a == t:
            v = "MATCH"
        else:
            v = "MISMATCH"
        print(f"{v:22} {path.relative_to(ROOT)}  ANTLR={a}  tomlc17={t}")
        rows.append({
            "file": str(path.relative_to(ROOT)), "group": name,
            "antlr": a, "tomlc17": t, "verdict": v,
            "antlr_stderr": ad, "tomlc17_result": td
        })
    return rows

def main():
    rows = []
    rows += group("VALID", (TEST_ROOT/"valid").glob("*.toml"), "ACCEPT", "ACCEPT")
    rows += group("INVALID", (TEST_ROOT/"invalid").glob("*.toml"), "REJECT", "REJECT")
    rows += group("SEMANTIC_INVALID", SEMANTIC_DIR.glob("*.toml"), "ACCEPT", "REJECT")
    if "--skip-boundary" not in sys.argv:
        rows += group("BOUNDARY", (TEST_ROOT/"boundary").glob("*.toml"), "ACCEPT", None)

    RESULT_FILE.write_text(json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8")
    counts = {}
    for r in rows: counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1
    print("\n=== SUMMARY ===")
    print(json.dumps(counts, indent=2))
    print(f"\nDetailed results: {RESULT_FILE}")
    return 1 if any(counts.get(k, 0) for k in
                     ("GRAMMAR_MISMATCH","MISMATCH","TOMLC17_UNEXPECTED")) else 0

if __name__ == "__main__":
    raise SystemExit(main())
