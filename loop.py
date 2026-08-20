"""
loop.py — Agentic fuzzing loop orchestrator.

Two phases, with deliberately different gate policies.

PHASE 1 — V1 bootstrap (HARD gate, up to MAX_V1_ATTEMPTS attempts)
    V1 is the architecture every later version inherits: strategy_generator
    sends the previous generator as the implementation baseline, so a
    structural defect in V1 propagates through V2..V5 rather than costing a
    single iteration. V1 also has no campaign feedback available, so the
    validation smoke test is the only diagnostic channel that exists.
    Each failed attempt therefore feeds its diagnosis into the next attempt;
    this is a correction loop, not a re-roll. If every attempt fails, the
    best-scoring attempt is kept and the run continues (a weak V1 is worth
    more than no run), with the shortfall recorded.

PHASE 2 — V2..V5 refinement (NO gate)
    Once a base exists, a weak version is recoverable: its 500-input campaign
    still produces evidence for the next version, and the campaign costs no
    tokens and no iteration. Discarding it would waste the only free
    diagnostic while the generation call has already been paid for. The
    20-input smoke test is skipped here as redundant — the campaign measures
    the same thing on 25x the data.

Budget is checked BEFORE each call. See budget.py.

Usage:
    python loop.py                     # full run: V1 bootstrap then V2..V5
    python loop.py --skip-v1-generate  # use existing generator_v1.py as base
    python loop.py --v1-only           # bootstrap V1 and stop
"""

import argparse
import importlib
import json
import shutil
import sys
import time
from pathlib import Path

from strategy_generator import generate_strategy
from validate_generator import (
    validate_generator, validate_version, load_valid_only_strategy,
    ACCEPTANCE_FLOOR, VALID_PATH_FLOOR, SMOKE_TEST_SIZE,
)
from campaign import run_campaign, shrink_all_crashes
from feedback import FeedbackAggregator
from budget import BudgetTracker, BudgetExceeded
from malformed_tracker import MalformedTracker
import structural_metrics
import capability_check

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
MAX_ITERATIONS = 5          # assignment constraint
MAX_COST_USD = 5.0           # assignment constraint
CAMPAIGN_SIZE = 500          # assignment constraint
FEEDBACK_BUDGET = 2         # signals sent to the LLM per iteration

# ---------------------------------------------------------------------------
# Thresholds are expressed against VALID-PATH acceptance rate, not the
# overall mixture. The overall rate mixes the valid path (which should be
# accepted) with the malformed path (which should be rejected), so a change
# in it is ambiguous and the two readings call for opposite corrections.
# The valid-path rate has an unambiguous target of 1.0: any shortfall is a
# defect with an actionable fix.
#
# Below this valid-path rate, the malformed-exploration channel is
# suppressed so it cannot compete with validity recovery. Set at 0.80
# because the valid path SHOULD approach 1.0 — a fifth of valid documents
# being rejected already means the generator is emitting invalid TOML by
# construction, which is a defect rather than a trade-off.
MALFORMED_CHANNEL_FLOOR = 0.80

# Attempts allowed for the V1 bootstrap. Bounded by budget rather than
# derived from it: each attempt is a fresh sample from a model with known
# high output variance, and attempts 2..N are diagnosis-driven rather than
# blind. Three gives the correction loop room to work without letting a
# failing seed consume the iterations reserved for refinement.
MAX_V1_ATTEMPTS = 3

# Sample drawn to measure the valid path each iteration. Separate from
# the 500-per-iteration campaign cap, which governs campaign examples.
VALID_PATH_SAMPLE = 50

# Starting ceiling for drawn nesting depth, and the hard cap the policy
# may raise it to. The generator discovers whether depth helps; these
# only bound what it is allowed to draw.
DEFAULT_MAX_DEPTH = 8
MAX_DEPTH_CEILING = 64


STRATEGIES_DIR = Path("strategies")
LOG_DIR = Path("logs")


# ---------------------------------------------------------------------------
# Feedback construction
# ---------------------------------------------------------------------------
def describe_gate_failure(stats, attempt=None):
    """Render one failed validation attempt as a diagnosis paragraph."""
    rate = stats.get("acceptance_rate", 0.0)
    floor = stats.get("floor", ACCEPTANCE_FLOOR)
    top = stats.get("top_rejections") or []

    header = f"Attempt {attempt}: " if attempt is not None else ""
    lines = [
        f"{header}acceptance rate {rate:.2f}, below the {floor:.2f} floor. "
        "Most generated documents were rejected by the parser before they "
        "could exercise it."
    ]

    if stats.get("error"):
        lines.append(f"Execution error: {stats['error']}")
        return "\n".join(lines)

    if top:
        breakdown = ", ".join(f"{cat} ({n})" for cat, n in top)
        lines.append(
            f"Rejections concentrated in: {breakdown}. Each category names "
            "the value strategy responsible."
        )

        numeric = {"FLOAT_PARSE", "INT_PARSE", "INVALID_VALUE"}
        if any(cat in numeric for cat, _ in top):
            lines.append(
                "Numeric rejection dominance means out-of-range values "
                "(integers outside int64, or 1e999) are being produced by "
                "valid_leaf(). valid_leaf() must contain ONLY values "
                "tomlc17 accepts. Those values belong in the _malformed_* "
                "strategies, which are not reachable from the recursive "
                "valid path."
            )

        string_cats = {
            "UNTERMINATED_STRING", "INVALID_STRING_CHAR", "BAD_ESCAPE",
            "BAD_HEX_ESCAPE", "UTF8_ERROR",
        }
        if any(cat in string_cats for cat, _ in top):
            lines.append(
                "String rejection dominance means the string strategies are "
                "emitting unescaped quotes, backslashes, or control "
                "characters. Escape string contents before wrapping them in "
                "delimiters."
            )

        semantic = {"DUPLICATE_KEY", "TABLE_CONFLICT", "INLINE_TABLE_EXTENSION"}
        if any(cat in semantic for cat, _ in top):
            lines.append(
                "Semantic-conflict dominance means valid_document() is "
                "reusing key or table names by accident. Track the names "
                "already emitted and never reuse one; those conflicts should "
                "appear only as deliberate malformed mutations."
            )

    return "\n".join(lines)


def build_v1_retry_feedback(history):
    """Build cumulative, escalating feedback from all failed V1 attempts.

    Later attempts see every earlier diagnosis, so the model is not free to
    cycle between two failure modes. The closing instruction sharpens once
    more than one attempt has failed.
    """
    parts = [describe_gate_failure(s, attempt=i) for i, s in history]

    if len(history) >= 2:
        parts.append(
            "More than one attempt has now failed. Work through the "
            "skeleton section by section and verify each one in isolation: "
            "for every branch of valid_leaf(), confirm the exact text it "
            "emits is accepted by a TOML 1.1 parser before including it. "
            "Do not restructure the fixed harness, and do not drop "
            "structural coverage to raise the acceptance rate."
        )

    return "\n\n".join(parts)


def compute_max_depth(observed_depth, current_max, valid_path_rate,
                      valid_path_rejections=None):
    """Derive the next MAX_DEPTH from the depth actually reached.

    Discovery stays with the loop: the target depth is never written into
    the prompt. What is controlled is only the CEILING on what the generator
    may draw.

    The parser itself supplies the stopping signal. When STACK_OVERFLOW
    appears among VALID-PATH rejections, the generator is producing
    documents too deep for tomlc17 to accept — those are abandoned at their
    first line and never build a structure at all, so further depth is
    strictly wasted. The productive zone is just BELOW that boundary, where
    the parser accepts the document and must allocate, populate and free
    the whole nested structure.

    This is black-box: the boundary is inferred from observed rejections,
    not read from the target's source.
    """
    observed = observed_depth or 0
    current = current_max or DEFAULT_MAX_DEPTH
    rejections = dict(valid_path_rejections or {})

    # The parser is refusing the depth being generated: pull back below it.
    overflow = rejections.get("STACK_OVERFLOW", 0)
    if overflow:
        # Retreat below the depth that provoked the refusal, so documents
        # land in the accepted-but-deep region rather than being rejected.
        retreat = max(DEFAULT_MAX_DEPTH, int(observed * 0.7) or DEFAULT_MAX_DEPTH)
        return min(retreat, current)

    if valid_path_rate is not None and valid_path_rate < MALFORMED_CHANNEL_FLOOR:
        return current

    # Pressing against the ceiling with everything still accepted.
    if observed >= current * 0.8:
        return min(current * 2, MAX_DEPTH_CEILING)

    # Far short of what is already allowed: the ceiling is not the limit.
    if observed < current * 0.3:
        return max(DEFAULT_MAX_DEPTH, current // 2)

    return current


def compute_malformed_weight(valid_path_rate):
    """Derive MALFORMED_WEIGHT from the VALID-PATH acceptance rate.

    Rebased from the overall mixture, which could not steer this: lowering
    the weight while the valid path was itself emitting invalid TOML moved
    the overall rate in the wrong direction, so the controller fought a
    problem it could not reach.

    The valid-path rate should approach 1.0. When it does, the campaign can
    afford malformed exploration; when it does not, the valid generator is
    defective and malformed budget spent on top of that is wasted.

    Thresholds are a judgment call to defend in the report.
    """
    if valid_path_rate >= 0.95:
        return 0.20
    elif valid_path_rate >= 0.85:
        return 0.15
    elif valid_path_rate >= 0.70:
        return 0.10
    else:
        return 0.05


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def load_strategy(version):
    """Import strategies/generator_v{version}.py and return toml_documents."""
    module_name = f"generator_v{version}"
    if "strategies" not in sys.path:
        sys.path.insert(0, "strategies")
    importlib.invalidate_caches()
    if module_name in sys.modules:
        del sys.modules[module_name]
    module = importlib.import_module(module_name)
    return module.toml_documents


def save_log(name, entry):
    LOG_DIR.mkdir(exist_ok=True)
    log_file = LOG_DIR / f"{name}.json"
    with open(log_file, "w", encoding="utf-8") as f:
        json.dump(entry, f, indent=2, default=str)
    print(f"Log saved: {log_file}", flush=True)


# ---------------------------------------------------------------------------
# PHASE 1 — V1 bootstrap (hard gate)
# ---------------------------------------------------------------------------
def bootstrap_v1(tracker, skip_generate=False):
    """Produce a V1 that clears the validation gate.

    Returns (strategy, log) where strategy is None only if no attempt was
    executable at all.
    """
    print(f"\n{'='*60}")
    print("  PHASE 1 — V1 BOOTSTRAP (hard gate)")
    print(f"{'='*60}", flush=True)

    log = {"attempts": [], "max_attempts": MAX_V1_ATTEMPTS}
    gen_file = STRATEGIES_DIR / "generator_v1.py"

    # Use an existing V1 without spending tokens.
    if skip_generate:
        if not gen_file.exists():
            print(f"ERROR: --skip-v1-generate but {gen_file} is missing.")
            return None, log
        print(f"Using existing {gen_file} (no LLM call).", flush=True)
        try:
            strategy = load_strategy(1)
        except Exception as e:
            print(f"Existing V1 will not import: {e}", flush=True)
            log["error"] = str(e)
            return None, log
        passed, stats = validate_version(1)
        log["attempts"].append({"attempt": 0, "source": "existing", **stats})
        print(
            f"Validation: {'PASS' if passed else 'FAIL'}  "
            f"gate_rate={stats.get('gate_rate')} "
            f"({'valid-path' if stats.get('valid_path_isolated') else 'MIXTURE - degraded'})",
            flush=True,
        )
        log["accepted_attempt"] = 0
        log["gate_passed"] = passed
        return strategy, log

    failure_history = []   # [(attempt_number, stats)]
    best = None            # (rate, attempt, strategy)

    for attempt in range(1, MAX_V1_ATTEMPTS + 1):
        print(f"\n--- V1 attempt {attempt}/{MAX_V1_ATTEMPTS} ---", flush=True)

        # Budget check BEFORE spending.
        try:
            tracker.assert_can_afford(f"V1 attempt {attempt}")
        except BudgetExceeded as e:
            print(f"Stopping V1 bootstrap: {e}", flush=True)
            log["stopped"] = "cost_budget"
            break

        retry_feedback = (
            build_v1_retry_feedback(failure_history)
            if failure_history else None
        )
        if retry_feedback:
            print("Injecting diagnosis from previous attempt(s).", flush=True)

        attempt_log = {"attempt": attempt}

        # --- generate ---
        try:
            _, result = generate_strategy(
                version=1,
                feedback=retry_feedback,
            )
            cost = tracker.record(result.get("usage"), f"v1_attempt_{attempt}")
            attempt_log["usage"] = result.get("usage")
            attempt_log["cost_usd"] = round(cost, 6)
            tracker.print_status()
        except Exception as e:
            err = f"generation failed: {type(e).__name__}: {e}"
            print(err, flush=True)
            attempt_log["error"] = err
            log["attempts"].append(attempt_log)
            failure_history.append((attempt, {"error": err,
                                              "acceptance_rate": 0.0}))
            continue

        # Keep every attempt's source for the report.
        if gen_file.exists():
            shutil.copy(gen_file, STRATEGIES_DIR / f"generator_v1_attempt{attempt}.py")

        # --- executability ---
        try:
            strategy = load_strategy(1)
        except Exception as e:
            err = f"import failed: {type(e).__name__}: {e}"
            print(err, flush=True)
            attempt_log["error"] = err
            log["attempts"].append(attempt_log)
            failure_history.append((attempt, {"error": err,
                                              "acceptance_rate": 0.0}))
            continue

        # --- validation gate ---
        # Gated on the VALID-PATH rate: the generator is reloaded with
        # MALFORMED_WEIGHT forced to 0 so only the branch that is supposed
        # to be accepted is measured. Gating the mixture against a
        # valid-path floor would penalise a generator for doing malformed
        # exploration correctly.
        passed, stats = validate_version(1)
        attempt_log.update(stats)
        log["attempts"].append(attempt_log)

        if not stats.get("valid_path_isolated"):
            print(
                "  NOTE: valid path could not be isolated "
                "(MALFORMED_WEIGHT absent or never read); "
                "gate fell back to the overall mixture.",
                flush=True,
            )

        rate = stats.get("gate_rate", 0.0)
        print(
            f"Validation: {'PASS' if passed else 'FAIL'}  "
            f"gate_rate={rate}  floor={stats.get('floor')}  "
            f"(mixture={stats.get('acceptance_rate')})",
            flush=True,
        )
        vp_rej = stats.get("valid_path_top_rejections")
        if vp_rej:
            print("    rejections in the VALID path (defects):")
            for cat, n in vp_rej:
                print(f"      {cat}: {n}")
        elif stats.get("top_rejections"):
            for cat, n in stats["top_rejections"]:
                print(f"    {cat}: {n}")

        if best is None or rate > best[0]:
            best = (rate, attempt, strategy)
            shutil.copy(gen_file, STRATEGIES_DIR / "generator_v1_best.py")

        if passed:
            print(f"\nV1 accepted on attempt {attempt}.", flush=True)
            log["accepted_attempt"] = attempt
            log["gate_passed"] = True
            return strategy, log

        failure_history.append((attempt, stats))

    # No attempt passed the gate.
    if best is not None:
        rate, attempt, strategy = best
        print(
            f"\nNo V1 attempt cleared the gate. Keeping the best "
            f"(attempt {attempt}, acceptance {rate:.2f}) and continuing.",
            flush=True,
        )
        # Restore the best attempt as the canonical V1.
        shutil.copy(STRATEGIES_DIR / "generator_v1_best.py", gen_file)
        log["accepted_attempt"] = attempt
        log["gate_passed"] = False
        log["kept_best_rate"] = rate
        return strategy, log

    print("\nNo V1 attempt was executable. Cannot continue.", flush=True)
    log["gate_passed"] = False
    return None, log


# ---------------------------------------------------------------------------
# PHASE 2 — V2..V5 refinement (no gate)
# ---------------------------------------------------------------------------
def run_refinement(strategy, tracker, feedback_agg, v1_log):
    """Campaign V1, then generate and campaign V2..V5."""
    evolution = []
    prev_malformed_weight = None
    prev_max_depth = DEFAULT_MAX_DEPTH

    # Adaptive state carried across iterations.
    malformed_tracker = MalformedTracker()
    prev_metrics = None        # structural reach of the previous version
    prev_capability = None     # capability report of the previous version
    generation_attempts = 0
    capability_retries = 0
    successful_campaigns = 0
    best_version = 1
    best_score = None

    def version_score(observation, valid_path_rate=None, metrics=None):
        """Rank versions for elitist refinement.

        Crashes first (the objective), then the ABSOLUTE structural reach of
        this version, then valid-path health.

        NOT new_kpath_count. "New" is measured against everything previous
        rounds accumulated, so V1 is scored against an empty baseline and
        every k-path it produces counts as new, while a later version is
        scored against everything already seen. The counts are therefore not
        comparable across iterations, and whichever version runs first wins
        permanently — which makes elitist selection a no-op. Observed
        directly: V1 scored 54 and V4 scored 1, so V1 remained "best" for a
        whole run and V5 refined from V1, discarding a V4 that had reached
        depth 30 at a 0.98 valid-path rate.

        kpath_round_counts is the set of distinct k-paths THIS round reached,
        independent of what earlier rounds saw, so it is comparable. Depth is
        included because reaching deep ACCEPTED structure is the objective:
        it is what forces the parser to allocate, populate, and free a real
        nested structure.
        """
        health = (
            valid_path_rate
            if valid_path_rate is not None
            else observation["validity_rate"]
        )
        reach = len(observation.get("kpath_round_counts") or {})
        depth = (metrics or {}).get("depth", {}).get("max", 0) or 0

        # Health is a GUARDRAIL, not an objective: it enters as a threshold,
        # not as a ranking key. Ranking on the rate itself would make a
        # shallow generator with a perfect valid path outrank a deep one with
        # a near-perfect path — V1 (health 1.00, depth 5) would beat V4
        # (health 0.98, depth 30), even though V4 is the better fuzzer. Once
        # a version is healthy enough to reach the parser's accepted path,
        # further health buys nothing; depth does.
        healthy = 1 if health >= MALFORMED_CHANNEL_FLOOR else 0

        return (
            observation["crashed"],   # the objective
            healthy,                  # guardrail, as a threshold
            depth,                    # deepest ACCEPTED structure reached
            reach,                    # distinct k-paths this round
        )

    # Feedback sections held separately so each reaches the model in its own
    # tagged block rather than as one concatenated instruction blob.
    pending_signals = None
    pending_malformed = None
    pending_structural = None
    pending_capability = None

    for version in range(1, MAX_ITERATIONS + 1):
        print(f"\n{'='*60}")
        print(f"  ITERATION {version}")
        print(f"{'='*60}", flush=True)

        iter_start = time.monotonic()
        log_entry = {"version": version}

        # --- generate (V2+ only; V1 already exists from bootstrap) ---
        if version > 1:
            retry_used = False
            candidate_ready = False

            while True:
                try:
                    tracker.assert_can_afford(
                        f"V{version} generation"
                    )
                except BudgetExceeded as e:
                    print(f"Stopping: {e}", flush=True)
                    log_entry["stopped"] = "cost_budget"
                    save_log(f"iteration_{version}", log_entry)
                    break

                print(
                    "Generating from previous version + feedback..."
                    + (" [CAPABILITY RETRY]" if retry_used else ""),
                    flush=True,
                )

                if prev_malformed_weight is not None:
                    print(
                        f"  MALFORMED_WEIGHT: "
                        f"{prev_malformed_weight:.2f}",
                        flush=True,
                    )

                try:
                    generation_attempts += 1
                    _, result = generate_strategy(
                        version=version,
                        campaign_signals=pending_signals,
                        malformed_feedback=pending_malformed,
                        structural_feedback=pending_structural,
                        capability_feedback=pending_capability,
                        malformed_weight=prev_malformed_weight,
                        max_depth=prev_max_depth,
                        base_version=best_version,
                    )

                    cost = tracker.record(
                        result.get("usage"),
                        f"generate_v{version}"
                        + ("_retry" if retry_used else ""),
                    )

                    log_entry["generation"] = {
                        "usage": result.get("usage"),
                        "cost_usd": round(cost, 6),
                        "malformed_weight_set": prev_malformed_weight,
                    "max_depth_set": prev_max_depth,
                        "sections_sent": result.get("sections_sent"),
                        "prompt_chars": result.get("prompt_chars"),
                        "base_version": best_version,
                        "retry_used": retry_used,
                    }

                    tracker.print_status()

                except Exception as e:
                    print(f"Generation FAILED: {e}", flush=True)
                    log_entry["generation"] = {
                        "error": str(e),
                        "retry_used": retry_used,
                    }
                    save_log(f"iteration_{version}", log_entry)
                    break

                # ---- executability gate ----
                try:
                    strategy = load_strategy(version)
                except Exception as e:
                    err = f"{type(e).__name__}: {e}"
                    print(
                        f"Import FAILED: {err}",
                        flush=True,
                    )

                    log_entry["exec_error"] = err

                    if not retry_used:
                        retry_used = True
                        capability_retries += 1
                        pending_capability = (
                            f"The previous generator did not import "
                            f"({err}). Regenerate the same version "
                            f"conservatively from the best working "
                            f"version and fix this execution error."
                        )
                        continue

                    save_log(
                        f"iteration_{version}",
                        log_entry,
                    )
                    break

                # ---- capability gate ----
                gen_path = (
                    STRATEGIES_DIR /
                    f"generator_v{version}.py"
                )

                candidate_capability = (
                    capability_check.check_capabilities(gen_path)
                )

                candidate_catastrophic, candidate_advisory = (
                    capability_check.classify(
                        candidate_capability,
                        prev_capability,
                    )
                )

                # Advisory losses do NOT reject: rejecting costs a whole
                # iteration (20% of the budget), and a partially reduced
                # generator still produces a campaign worth measuring. The
                # loss is carried forward as feedback instead.
                if candidate_advisory and not candidate_catastrophic:
                    print(
                        "\nCapability loss (advisory) — running campaign "
                        "anyway:",
                        flush=True,
                    )
                    for a in candidate_advisory:
                        print(f"  - {a}", flush=True)
                    log_entry["capability_advisory"] = candidate_advisory

                candidate_regressions = candidate_catastrophic

                if candidate_regressions:
                    print(
                        "\nCAPABILITY REGRESSION "
                        "— candidate rejected:",
                        flush=True,
                    )

                    for regression in candidate_regressions:
                        print(
                            f"  - {regression}",
                            flush=True,
                        )

                    log_entry[
                        "pre_campaign_capability_regressions"
                    ] = candidate_regressions

                    if not retry_used:
                        retry_used = True

                        pending_capability = (
                            "The generated candidate has capability "
                            "regressions. Regenerate the same version "
                            "conservatively from the best working "
                            "version. Preserve all existing generator "
                            "capabilities and restore these regressions:\n"
                            + "\n".join(
                                f"- {r}"
                                for r in candidate_regressions
                            )
                        )

                        continue

                    print(
                        "Capability regression remains after one "
                        "retry; rejecting candidate.",
                        flush=True,
                    )

                    log_entry[
                        "candidate_rejected"
                    ] = "capability_regression"

                    save_log(
                        f"iteration_{version}",
                        log_entry,
                    )
                    break

                # Candidate passed all pre-campaign gates.
                candidate_ready = True
                break
            if not candidate_ready:
                log_entry["campaign_skipped"] = True
                save_log(f"iteration_{version}", log_entry)
                continue

            else:
                pass
        else:
            log_entry["bootstrap"] = {
                "gate_passed": v1_log.get("gate_passed"),
                "accepted_attempt": v1_log.get("accepted_attempt"),
                "attempts_used": len(v1_log.get("attempts", [])),
            }

        tracker.iterations_used = version

    
        # --- campaign (Pass A) ---
        print(f"\nRunning {CAMPAIGN_SIZE}-input campaign...", flush=True)
        successful_campaigns += 1
        records = run_campaign(strategy, n=CAMPAIGN_SIZE)

        # --- valid-path measurement ------------------------------------
        # Measured on its own sample with MALFORMED_WEIGHT forced to 0, so
        # the number reflects only the branch that is SUPPOSED to be
        # accepted. This is what the gate and the weight policy steer on.
        valid_path_rate = None
        valid_path_stats = None
        try:
            vp_strategy, knob_wired = load_valid_only_strategy(version)
            if knob_wired:
                _, valid_path_stats = validate_generator(
                    vp_strategy,
                    n=VALID_PATH_SAMPLE,
                    floor=VALID_PATH_FLOOR,
                )
                valid_path_rate = valid_path_stats["acceptance_rate"]
                print(
                    f"\nvalid-path rate = {valid_path_rate:.3f} "
                    f"(measured with MALFORMED_WEIGHT forced to 0)"
                )
                if valid_path_stats.get("top_rejections"):
                    print("  rejections inside the VALID path — these are "
                          "defects, not exploration:")
                    for cat, c in valid_path_stats["top_rejections"]:
                        print(f"    {cat}: {c}")
            else:
                print(
                    "\nWARNING: MALFORMED_WEIGHT is absent or never read, so "
                    "the valid path cannot be isolated. Falling back to the "
                    "overall mixture, which cannot distinguish working "
                    "malformed exploration from a broken valid generator.",
                    flush=True,
                )
        except Exception as e:
            print(f"\n(valid-path measurement failed: {e})", flush=True)

        log_entry["valid_path"] = {
            "rate": valid_path_rate,
            "isolated": valid_path_rate is not None,
            "top_rejections": (
                valid_path_stats.get("top_rejections")
                if valid_path_stats else None
            ),
        }
        log_entry["campaign"] = {"records_count": len(records)}

        # --- feedback ---
        observation = feedback_agg.add_round(records)
        signals = feedback_agg.select_signals(
            observation, max_signals=FEEDBACK_BUDGET
        )

        # Structural reach of ACCEPTED inputs only. Computed here because
        # elitist scoring needs it: depth reached through the accepted path
        # is the objective, since that is what makes the parser allocate,
        # populate and free a real nested structure.
        accepted_records = [
            r for r in records
            if r.get("report", {}).get("termination") == "NORMAL"
        ]
        metrics = structural_metrics.measure_campaign(accepted_records)

        current_score = version_score(observation, valid_path_rate, metrics)

        if best_score is None or current_score > best_score:
            best_score = current_score
            best_version = version
            log_entry["best_version"] = best_version
            log_entry["best_score"] = best_score
        else:
            log_entry["best_version"] = best_version
            log_entry["best_score"] = best_score
            log_entry["version_not_best"] = True
        llm_feedback = feedback_agg.build_llm_feedback(signals, observation)

        log_entry["observation"] = {
            "accepted": observation["accepted"],
            "rejected": observation["rejected"],
            "crashed": observation["crashed"],
            "acceptance_rate": round(observation["acceptance_rate"], 3),
            "validity_rate": round(observation["validity_rate"], 3),
            "new_kpath_count": observation["new_kpath_count"],
            "rare_kpath_count": observation["rare_kpath_count"],
        }
        log_entry["signals"] = [
            {"type": s.get("type"), "priority": s.get("priority")}
            for s in signals
        ]
        log_entry["instructions"] = llm_feedback["instructions"]

        print(f"\naccepted   = {observation['accepted']}")
        print(f"rejected   = {observation['rejected']}")
        print(f"crashed    = {observation['crashed']}")
        print(f"validity   = {observation['validity_rate']:.3f}")
        print(f"new kpaths = {observation['new_kpath_count']}")
        print("\nSelected signals:")
        for s in signals:
            print(f"  {s.get('type')}  priority={s.get('priority')}")

        # --- adaptive analysis ------------------------------------------
        # (a) Malformed exploration: which rejection categories remain
        #     unreached. Policy is exploration-first (see malformed_tracker).
        round_categories = malformed_tracker.add_round(records)

        # Keep measuring malformed coverage every round, but suppress
        # malformed instructions when valid generation is unhealthy.
        # Gate on the VALID-PATH rate when it could be isolated. Falling back
        # to the overall mixture is a degraded mode: the mixture cannot
        # distinguish "malformed exploration is working" from "the valid
        # generator is broken", which is the ambiguity this rebase removes.
        gate_rate = valid_path_rate
        gate_basis = "valid_path"
        if gate_rate is None:
            gate_rate = observation["validity_rate"]
            gate_basis = "overall_mixture(degraded)"

        if gate_rate >= MALFORMED_CHANNEL_FLOOR:
            malformed_instruction = malformed_tracker.build_instruction()
            log_entry["malformed_channel"] = f"active ({gate_basis})"
        else:
            malformed_instruction = ""
            log_entry["malformed_channel"] = (
                f"suppressed: {gate_basis} rate {gate_rate:.3f} "
                f"< floor {MALFORMED_CHANNEL_FLOOR:.2f}"
            )
            print(
                f"\nMalformed channel SUPPRESSED "
                f"({gate_basis} {gate_rate:.3f} < {MALFORMED_CHANNEL_FLOOR:.2f})",
                flush=True,
            )
        log_entry["gate_rate"] = round(gate_rate, 3)
        log_entry["gate_basis"] = gate_basis

        mt_summary = malformed_tracker.summary()
        log_entry["malformed_exploration"] = {
            "this_round": dict(round_categories),
            "categories_reached": mt_summary["categories_reached"],
            "categories_total": mt_summary["categories_total"],
            "coverage_fraction": mt_summary["coverage_fraction"],
            "unexplored": malformed_tracker.unexplored(),
        }

        # (b) Structural reach — metrics computed above for scoring.
        structural_instruction = structural_metrics.build_instruction(
            metrics, previous=prev_metrics
        )
        log_entry["structural_metrics"] = metrics

        # (c) Capability regression against the previous version's source.
        gen_path = STRATEGIES_DIR / f"generator_v{version}.py"
        capability_instruction = ""
        capability = None
        if gen_path.exists():
            capability = capability_check.check_capabilities(gen_path)
            cat_reg, adv_reg = capability_check.classify(
                capability, prev_capability
            )
            regressions = cat_reg + adv_reg
            capability_instruction = capability_check.build_instruction(
                capability, regressions
            )
            log_entry["capability"] = {
                "malformed_count": capability.get("malformed_count"),
                "missing_core": capability.get("missing_core"),
                "missing_malformed": capability.get("missing_malformed"),
                "uses_st_recursive": capability.get("uses_st_recursive"),
                "forbidden_imports": capability.get("forbidden_imports"),
                "regressions": regressions,
            }
            if regressions:
                print("\nCAPABILITY REGRESSION:")
                for r in regressions:
                    print(f"  - {r}")

        print(
            f"\nstructural reach: depth max "
            f"{metrics.get('depth', {}).get('max', '?')}, "
            f"container max "
            f"{metrics.get('container_size', {}).get('max', '?')}"
        )
        print(
            f"rejection categories reached: "
            f"{mt_summary['categories_reached']}/"
            f"{mt_summary['categories_total']} "
            f"({mt_summary['coverage_fraction']:.1%})"
        )

        # --- shrink (Pass B) ---
        shrunk = shrink_all_crashes(strategy, records)
        if shrunk:
            log_entry["crashes"] = {}
            for sig, data in shrunk.items():
                log_entry["crashes"][sig] = {
                    "original_len": len(data["original"]),
                    "shrunk_len": len(data["shrunk"]),
                    "shrunk_input": data["shrunk"][:500],
                }
                print(f"\nCrash: {sig}")
                print(f"  Shrunk to {len(data['shrunk'])} chars")

        # --- prepare next iteration ---
        prev_malformed_weight = compute_malformed_weight(gate_rate)

        # Depth ceiling from the depth ACCEPTED inputs actually reached.
        # Valid-path rejections tell the policies where the parser's own
        # limits are: a boundary observed, not read from its source.
        vp_rejections = dict(
            (valid_path_stats or {}).get("top_rejections") or []
        )

        observed_depth = (metrics or {}).get("depth", {}).get("max", 0)
        prev_max_depth = compute_max_depth(
            observed_depth, prev_max_depth, valid_path_rate, vp_rejections
        )
        log_entry["next_max_depth"] = prev_max_depth
        note = ""
        if vp_rejections.get("STACK_OVERFLOW"):
            note = (f"  [backing off: {vp_rejections['STACK_OVERFLOW']} "
                    "STACK_OVERFLOW in the valid path]")
        print(f"Next MAX_DEPTH: {prev_max_depth} "
              f"(accepted depth reached {observed_depth}){note}")

        log_entry["next_malformed_weight"] = prev_malformed_weight

        # Hold each section separately; strategy_generator renders them into
        # their own tagged blocks, ordered by how directly each constrains
        # the next version (capability first — a dropped strategy is a vector
        # the campaign cannot reach at all).
        # Rejections inside the valid path are defects, not exploration.
        # Named explicitly, because the model cannot otherwise tell them
        # apart from deliberate malformed output.
        if valid_path_rate is not None and valid_path_rate < VALID_PATH_FLOOR:
            cats = ""
            if valid_path_stats and valid_path_stats.get("top_rejections"):
                cats = ", ".join(
                    f"{c} ({n})"
                    for c, n in valid_path_stats["top_rejections"]
                )
            defect = (
                f"VALID-PATH DEFECT: with MALFORMED_WEIGHT forced to 0 — so "
                f"every input came from the valid path — only "
                f"{valid_path_rate:.0%} were accepted. The valid generator is "
                f"emitting TOML the parser rejects."
            )
            if cats:
                defect += (
                    f" Rejections: {cats}. Semantic categories here "
                    "(DUPLICATE_KEY, TABLE_CONFLICT, INLINE_TABLE_EXTENSION) "
                    "mean valid_document() is reusing names by accident: keys "
                    "and table headers must be unique BY CONSTRUCTION, not by "
                    "hoping independent draws differ."
                )
            defect += (
                " Fix this before any other change. A rejected document is "
                "abandoned at its first bad line, so everything after it is "
                "never parsed and no deep structure is reached."
            )
            capability_instruction = (
                defect + "\n\n" + capability_instruction
                if capability_instruction else defect
            )

        pending_capability = capability_instruction or None
        pending_signals = llm_feedback["instructions"] or None
        pending_structural = structural_instruction or None
        pending_malformed = malformed_instruction or None

        log_entry["feedback_sections"] = {
            "capability": bool(pending_capability),
            "signals": len(llm_feedback["instructions"]),
            "structural": bool(pending_structural),
            "malformed_exploration": bool(pending_malformed),
        }

        prev_metrics = metrics
        prev_capability = capability

        print(f"\nNext MALFORMED_WEIGHT: {prev_malformed_weight:.2f}")

        evolution.append({
            "version": version,
            "accepted": observation["accepted"],
            "rejected": observation["rejected"],
            "crashed": observation["crashed"],
            "validity_rate": round(observation["validity_rate"], 3),
            "valid_path_rate": valid_path_rate,
            "new_kpaths": observation["new_kpath_count"],
            "signals": [s.get("type") for s in signals],
            "crash_count": len(shrunk) if shrunk else 0,
            "malformed_weight": prev_malformed_weight,
            "max_depth": metrics.get("depth", {}).get("max"),
            "max_container": metrics.get("container_size", {}).get("max"),
            "categories_reached": mt_summary["categories_reached"],
            "capability_regressions": (
                log_entry.get("capability", {}).get("regressions") or []
            ),
        })

        log_entry["elapsed_seconds"] = round(time.monotonic() - iter_start, 1)
        log_entry["budget"] = {
            "cost_usd": round(tracker.cost_usd, 6),
            "total_tokens": tracker.total_tokens,
        }
        save_log(f"iteration_{version}", log_entry)
        print(f"\nIteration {version} complete in "
              f"{log_entry['elapsed_seconds']}s")

        if tracker.exhausted():
            print("Cost budget exhausted. Stopping.", flush=True)
            break

    return evolution, malformed_tracker


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def run_loop(skip_v1_generate=False, v1_only=False):
    tracker = BudgetTracker(
        max_cost_usd=MAX_COST_USD, max_iterations=MAX_ITERATIONS
    )

    if not tracker.pricing_is_configured():
        print(
            "WARNING: token pricing in budget.py is all zeros, so cost "
            "tracking is inactive and the $5 cap will never trigger. Token "
            "counts are still recorded. Set PRICE_PER_1M_* before the final "
            "run.\n",
            flush=True,
        )

    strategy, v1_log = bootstrap_v1(tracker, skip_generate=skip_v1_generate)
    save_log("v1_bootstrap", v1_log)

    if strategy is None:
        print("\nAborting: no usable V1.", flush=True)
        tracker.save(LOG_DIR / "budget.json")
        return []

    if v1_only:
        print("\n--v1-only set; stopping after bootstrap.", flush=True)
        tracker.save(LOG_DIR / "budget.json")
        return []

    feedback_agg = FeedbackAggregator()
    evolution, malformed_tracker = run_refinement(
        strategy, tracker, feedback_agg, v1_log
    )

    # --- final summary ---
    print(f"\n{'='*60}")
    print("  LOOP COMPLETE")
    print(f"{'='*60}")
    summary = tracker.summary()
    successful_campaigns = len(evolution)

    print(
        f"Campaign iterations:  {successful_campaigns}/{MAX_ITERATIONS}"
    )
    print(f"Generation attempts:  {len(tracker.calls)}")
    print(f"Total tokens:         {summary['total_tokens']}")
    print(f"  input:              {summary['input_tokens']}")
    print(f"  output:             {summary['output_tokens']}")
    print(f"  thought:            {summary['thought_tokens']}")
    print(f"Total cost:           ${summary['cost_usd']:.4f} "
          f"of ${MAX_COST_USD:.2f}")

    if evolution:
        print("\nEvolution:")
        for e in evolution:
            print(
                f"  V{e['version']}: "
                f"valid_path="
                f"{e.get('valid_path_rate') if e.get('valid_path_rate') is not None else float('nan'):.3f}  "
                f"mixture={e['validity_rate']:.3f}  "
                f"crashed={e['crashed']}  "
                f"new_kpaths={e['new_kpaths']}  "
                f"depth={e.get('max_depth')}  "
                f"container={e.get('max_container')}  "
                f"categories={e.get('categories_reached')}  "
                f"weight={e['malformed_weight']:.2f}"
            )
            if e.get("capability_regressions"):
                for r in e["capability_regressions"]:
                    print(f"        regression: {r}")

    # Category coverage: which rejection categories were never reached.
    # Categories are never retired, so this list is the evidence-backed
    # answer to "which parts of the grammar remain under-tested".
    malformed_tracker.print_final_report()

    LOG_DIR.mkdir(exist_ok=True)
    with open(LOG_DIR / "category_coverage.json", "w", encoding="utf-8") as f:
        json.dump(malformed_tracker.final_report(), f, indent=2, default=str)

    with open(LOG_DIR / "evolution.json", "w", encoding="utf-8") as f:
        json.dump({
            "iterations": evolution,
            "v1_bootstrap": {
                "gate_passed": v1_log.get("gate_passed"),
                "attempts_used": len(v1_log.get("attempts", [])),
                "accepted_attempt": v1_log.get("accepted_attempt"),
            },
            "budget": summary,
        }, f, indent=2, default=str)
    tracker.save(LOG_DIR / "budget.json")

    return evolution


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Run the agentic fuzzing loop."
    )
    parser.add_argument(
        "--skip-v1-generate", action="store_true",
        help="Use the existing strategies/generator_v1.py instead of "
             "generating one (spends no tokens on V1).",
    )
    parser.add_argument(
        "--v1-only", action="store_true",
        help="Bootstrap V1 and stop, without running any campaign.",
    )
    args = parser.parse_args()
    run_loop(
        skip_v1_generate=args.skip_v1_generate,
        v1_only=args.v1_only,
    )
