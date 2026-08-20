"""
campaign.py — @given-based fuzzing campaign runner.

Pass A (run_campaign): collects all results from a bounded @given run.
    Uses @given/@settings(max_examples=500) as the assignment requires.
    Never raises, so every input is logged. Returns the records list that
    FeedbackAggregator.add_round() consumes.

Pass B (shrink_crash): for each unique crash signature found in Pass A,
    uses hypothesis.find() to locate the smallest reproducer. find() uses
    Hypothesis's built-in shrinker, satisfying the Step 5.4 requirement.
    Falls back to the original crashing input if re-discovery fails.

shrink_all_crashes: convenience wrapper that runs Pass B for every unique
    crash signature extracted from Pass A records.
"""

import tempfile
import time
from pathlib import Path
from collections import defaultdict

from hypothesis import given, settings, HealthCheck, find
from hypothesis.errors import NoSuchExample

from runner import run_harness
from triage import triage


# ---------------------------------------------------------------------------
# Pass A — collection run
# ---------------------------------------------------------------------------
def run_campaign(strategy, n=500, max_duration=600):
    """Run n examples through the harness via @given, collecting every result.

    Returns list of {"input": str, "report": <triage output>}.
    The 10-minute max_duration is a wall-clock backstop; remaining examples
    are skipped (not processed) once it fires.
    """
    records = []
    start = time.monotonic()
    counter = [0]
    timed_out = [False]
    tmpdir_obj = tempfile.TemporaryDirectory(prefix="toml_campaign_")
    tmpdir = Path(tmpdir_obj.name)

    @given(strategy)
    @settings(
        max_examples=n,
        deadline=None,
        database=None,
        suppress_health_check=list(HealthCheck),
    )
    def collect(text):
        # Wall-clock backstop: skip remaining examples.
        if timed_out[0]:
            return
        if time.monotonic() - start >= max_duration:
            timed_out[0] = True
            print(
                "Campaign stopped: 10-minute wall-clock limit reached.",
                flush=True,
            )
            return

        if not isinstance(text, str):
            text = str(text)

        idx = counter[0]
        counter[0] += 1
        input_file = tmpdir / f"input_{idx:04d}.toml"

        try:
            input_file.write_text(
                text, encoding="utf-8", errors="surrogatepass"
            )
        except UnicodeEncodeError as e:
            records.append({
                "input": text,
                "report": {
                    "status": "NON_UTF8",
                    "termination": "NON_UTF8",
                    "error": str(e),
                },
            })
            return

        result = run_harness(str(input_file))
        report = triage(result)
        records.append({"input": text, "report": report})

    collect()
    tmpdir_obj.cleanup()

    elapsed = time.monotonic() - start
    print(
        f"Campaign complete: {len(records)} records in {elapsed:.1f}s",
        flush=True,
    )
    return records


# ---------------------------------------------------------------------------
# Pass B — per-signature shrinking
# ---------------------------------------------------------------------------
def shrink_crash(strategy, target_signature, fallback_input,
                 max_examples=200):
    """Find the smallest input from strategy that crashes with target_signature.

    Uses hypothesis.find() which applies Hypothesis's built-in shrinker.
    If the crash cannot be re-discovered within max_examples, returns
    fallback_input (the original crashing input from Pass A).
    """
    tmpdir_obj = tempfile.TemporaryDirectory(prefix="toml_shrink_")
    tmpdir = Path(tmpdir_obj.name)
    counter = [0]

    def crashes_with_sig(text):
        if not isinstance(text, str):
            text = str(text)
        idx = counter[0]
        counter[0] += 1
        input_file = tmpdir / f"shrink_{idx:04d}.toml"
        try:
            input_file.write_text(
                text, encoding="utf-8", errors="surrogatepass"
            )
        except UnicodeEncodeError:
            return False
        result = run_harness(str(input_file))
        report = triage(result)
        return report.get("signature") == target_signature

    try:
        minimal = find(
            strategy,
            crashes_with_sig,
            settings=settings(
                max_examples=max_examples,
                deadline=None,
                database=None,
                suppress_health_check=list(HealthCheck),
            ),
        )
    except (NoSuchExample, Exception):
        # Could not re-discover the crash; keep the original input.
        minimal = fallback_input
    finally:
        tmpdir_obj.cleanup()

    return minimal


def shrink_all_crashes(strategy, records, max_examples_per_sig=200):
    """Run Pass B for every unique crash signature in Pass A records.

    Returns dict: {signature: {"original": str, "shrunk": str}}.
    """
    crash_terminations = {"SANITIZER", "TIMEOUT", "ABNORMAL"}

    # Group crashing inputs by signature; keep the shortest original.
    by_sig = defaultdict(list)
    for rec in records:
        rep = rec["report"]
        if rep.get("termination") in crash_terminations:
            sig = rep.get("signature")
            if sig:
                by_sig[sig].append(rec["input"])

    if not by_sig:
        print("No crashes to shrink.", flush=True)
        return {}

    results = {}
    for sig, inputs in by_sig.items():
        # Use the shortest original as fallback.
        fallback = min(inputs, key=len)
        print(f"Shrinking signature: {sig[:60]}...", flush=True)
        shrunk = shrink_crash(
            strategy, sig, fallback,
            max_examples=max_examples_per_sig,
        )
        results[sig] = {"original": fallback, "shrunk": shrunk}
        if shrunk != fallback:
            print(
                f"  Shrunk: {len(fallback)} -> {len(shrunk)} chars",
                flush=True,
            )
        else:
            print("  Could not shrink further.", flush=True)

    return results


# ---------------------------------------------------------------------------
# Standalone test
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys

    sys.path.insert(0, "strategies")
    from generator_v1 import toml_documents
    from feedback import FeedbackAggregator

    # Pass A: 500-input campaign.
    print("=== PASS A: COLLECTION ===")
    records = run_campaign(strategy=toml_documents, n=500)
    print(f"Generated records: {len(records)}")

    # Aggregate results.
    feedback = FeedbackAggregator()
    observation = feedback.add_round(records)

    print(f"\naccepted  = {observation['accepted']}")
    print(f"rejected  = {observation['rejected']}")
    print(f"crashed   = {observation['crashed']}")
    print(f"validity  = {observation['validity_rate']:.3f}")
    print(f"new kpaths = {observation['new_kpath_count']}")

    # Pass B: shrink any crashes found.
    print("\n=== PASS B: SHRINKING ===")
    shrunk = shrink_all_crashes(toml_documents, records)
    for sig, data in shrunk.items():
        print(f"\nSignature: {sig}")
        print(f"  Original ({len(data['original'])} chars): {data['original'][:80]!r}")
        print(f"  Shrunk   ({len(data['shrunk'])} chars): {data['shrunk'][:80]!r}")

    # Feedback signals (for iteration 2).
    signals = feedback.select_signals(observation, max_signals=3)
    llm_feedback = feedback.build_llm_feedback(signals, observation)

    print("\n=== SELECTED SIGNALS ===")
    for s in signals:
        print(f"  {s.get('type')}  priority={s.get('priority')}")

    print("\n=== LLM FEEDBACK ===")
    for inst in llm_feedback["instructions"]:
        print(f"  - {inst}")
