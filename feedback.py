

from collections import Counter, defaultdict

DEFAULT_FEEDBACK_BUDGET = 2


# ==========================================================================
# 1. k-path structural diversity  (input-side proxy; NOT code coverage)
# ==========================================================================
def _token_structure(text):
    """Light, dependency-free structural tokenization of a TOML string.
    Approximate on purpose — a diversity proxy, not a validator."""
    events = []
    depth = 0
    i, n = 0, len(text)
    line_start = True
    while i < n:
        c = text[i]
        if c == '\n':
            line_start = True; i += 1; continue
        if c in ' \t':
            i += 1; continue
        if c == '#':
            events.append(("comment", depth))
            while i < n and text[i] != '\n':
                i += 1
            continue
        if line_start and c == '[':
            if text[i:i+2] == '[[':
                events.append(("array_table", depth)); i += 2
            else:
                events.append(("std_table", depth)); i += 1
            line_start = False
            continue
        line_start = False
        if c == '[':
            depth += 1; events.append(("array_open", depth)); i += 1; continue
        if c == ']':
            events.append(("array_close", depth)); depth = max(0, depth-1); i += 1; continue
        if c == '{':
            depth += 1; events.append(("inline_table_open", depth)); i += 1; continue
        if c == '}':
            events.append(("inline_table_close", depth)); depth = max(0, depth-1); i += 1; continue
        if c == '"':
            if text[i:i+3] == '"""':
                events.append(("ml_basic_string", depth)); i += 3
            else:
                events.append(("basic_string", depth)); i += 1
            continue
        if c == "'":
            if text[i:i+3] == "'''":
                events.append(("ml_literal_string", depth)); i += 3
            else:
                events.append(("literal_string", depth)); i += 1
            continue
        if c == '=':
            events.append(("keyval", depth)); i += 1; continue
        if c == ',':
            events.append(("comma", depth)); i += 1; continue
        if c == '.':
            events.append(("dotted_key", depth)); i += 1; continue
        i += 1
    return events


def k_paths(text, k=2):
    """Set of k-paths (production tuples of length 1..k). k=1 => which productions
    appeared; k>=2 => production-in-context. Purely input-side."""
    tags = [t for (t, _d) in _token_structure(text)]
    paths = set()
    for length in range(1, k + 1):
        for idx in range(len(tags) - length + 1):
            paths.add(tuple(tags[idx: idx + length]))
    return paths


def structural_fingerprint(text):
    """Coarse counts for reporting / rejection-context labelling."""
    events = _token_structure(text)
    tags = [t for (t, _d) in events]
    max_depth = max((d for (_t, d) in events), default=0)
    c = Counter(tags)
    return {
        "max_depth": max_depth,
        "arrays": c.get("array_open", 0),
        "inline_tables": c.get("inline_table_open", 0),
        "std_tables": c.get("std_table", 0),
        "array_tables": c.get("array_table", 0),
        "strings": (c.get("basic_string", 0) + c.get("literal_string", 0)
                    + c.get("ml_basic_string", 0) + c.get("ml_literal_string", 0)),
        "dotted_keys": c.get("dotted_key", 0),
        "comments": c.get("comment", 0),
    }


# ==========================================================================
# 1b. Human-readable translation + example anchoring for LLM-facing feedback.
#     (Communication layer only — selection is unchanged.)
# ==========================================================================
_CONCEPT_MAP = {
    "array_open": "an array", "array_close": "an array end",
    "inline_table_open": "an inline table", "inline_table_close": "an inline table end",
    "std_table": "a table header", "array_table": "an array-of-tables header",
    "keyval": "a key/value assignment", "dotted_key": "a dotted key",
    "comma": "a separator", "comment": "a comment",
    "basic_string": "a basic string", "literal_string": "a literal string",
    "ml_basic_string": "a multi-line basic string", "ml_literal_string": "a multi-line literal string",
}


def _humanize_kpath(kp):
    semantic_pairs = {
        ("array_open", "array_open"): "nested arrays",
        ("array_open", "array_close"): "an array containing a value or nested structure",
        ("array_close", "array_close"): "nested array closure",
        ("inline_table_open", "inline_table_open"): "nested inline tables",
        ("inline_table_open", "inline_table_close"): "a simple inline table",
        ("inline_table_close", "inline_table_close"): "nested inline-table closure",
        ("keyval", "array_open"): "a key assigned an array value",
        ("keyval", "inline_table_open"): "a key assigned an inline-table value",
        ("keyval", "dotted_key"): "a key-value assignment using a dotted key",
    }

    if not kp:
        return "a TOML structural pattern"

    if len(kp) == 2 and tuple(kp) in semantic_pairs:
        return semantic_pairs[tuple(kp)]

    return " followed by ".join(
        _CONCEPT_MAP.get(t, t) for t in kp
    )


def _kpath_concepts(examples, max_n=2):
    seen = []
    for kp in examples[:max_n]:
        phrase = _humanize_kpath(kp)
        if phrase not in seen:
            seen.append(phrase)
    return "; ".join(seen)


def _clip(text, limit=140):
    if not text:
        return text
    t = text.replace("\n", "\\n")
    return t if len(t) <= limit else t[:limit] + "\u2026"


# ==========================================================================
# 2. FeedbackAggregator — accumulates one fuzzing round, tracks history.
# ==========================================================================
class FeedbackAggregator:
    def __init__(self, kpath_k=2, rare_threshold=2):
        self.kpath_k = kpath_k
        self.rare_threshold = rare_threshold      # [FIX] cumulative count <= this => "rare"
        self.round_index = 0
        # cross-round memory
        self.seen_kpaths = set()                  # k-paths ever seen (presence)
        self.kpath_counts = Counter()             # [FIX] cumulative k-path frequency
        self.seen_reject_categories = set()
        self.seen_crash_signatures = set()
        self.reject_context_history = defaultdict(set)
        self.prev_reject_rates = {}
        self.history = []

    @staticmethod
    def _rank_kpaths_for_llm(kpaths, limit=3):
                """Prefer semantically meaningful TOML structures."""
                structural = {
                    "array_open",
                    "array_table",
                    "inline_table_open",
                    "keyval",
                    "dotted_key",
                    "literal_string",
                    "ml_literal_string",
                    "basic_string",
                }
    
                def score(kp):
                    score = 0
    
                    if len(kp) >= 2:
                        score += 3
    
                    score += sum(2 for token in kp if token in structural)
    
                    if len(kp) == 1 and kp[0] in {
                        "array_open",
                        "array_close",
                        "comma",
                        "comment",
                        "inline_table_close",
                    }:
                        score -= 3
    
                    return score
    
                return sorted(kpaths, key=score, reverse=True)[:limit]

    def add_round(self, records):
        """records: [{"input": str, "report": <triage.triage() output>}].
        Returns a round observation with novelty/trend/rarity already marked."""
        self.round_index += 1
        total = len(records)
        accepted = rejected = crashed = 0

        reject_counter = Counter()
        reject_contexts = defaultdict(set)
        new_kpaths_this_round = set()
        seen_this_round_kp = set()                # [FIX] all k-paths observed this round
        kpath_round_counts = Counter()              # frequency baseline: current-round count
        crash_candidates = []
        reject_examples = {}                        # [PROMPT] one example input per reject category
        accepted_samples = []                       # [PROMPT] (input, kpaths) for example anchoring
        new_kpath_example = None                    # [PROMPT] a valid input that reached new structure

        for rec in records:
            rep = rec["report"]
            term = rep.get("termination")

            if term in ("SANITIZER", "TIMEOUT", "ABNORMAL"):
                crashed += 1
                sig = rep.get("signature")
                is_new = sig not in self.seen_crash_signatures
                if sig:
                    self.seen_crash_signatures.add(sig)
                crash_candidates.append({
                    "signature": sig, "termination": term,
                    "bug_type": rep.get("bug_type"), "signal": rep.get("signal"),
                    "new": is_new, "similar": rep.get("similar_crashes", []),
                    "example_input": rec.get("input"),   # [PROMPT]
                })

            elif term == "REJECT":
                rejected += 1
                cat = (rep.get("rejection") or {}).get("error_type", "UNKNOWN")
                reject_counter[cat] += 1
                reject_contexts[cat].add(self._reject_context(rec["input"]))
                reject_examples.setdefault(cat, rec["input"])   # [PROMPT]

            elif term == "NORMAL":
                accepted += 1
                kp = k_paths(rec["input"], self.kpath_k)
                fresh = kp - self.seen_kpaths
                new_kpaths_this_round |= fresh
                seen_this_round_kp |= kp
                if fresh and new_kpath_example is None:      # [PROMPT]
                    new_kpath_example = rec["input"]
                if len(accepted_samples) < 20:               # [PROMPT] cap memory
                    accepted_samples.append((rec["input"], kp))
                for p in kp:                       # [FIX] track cumulative frequency
                    self.kpath_counts[p] += 1
                    kpath_round_counts[p] += 1
                self.seen_kpaths |= kp
            # HARNESS_ERROR: ignored (not a target signal)

        # [FIX] RARE = seen-before-but-seldom: observed this round, NOT new this
        # round, and cumulative count still <= rare_threshold. Disjoint from NEW.
        rare_kpaths = {
            p for p in seen_this_round_kp
            if p not in new_kpaths_this_round
            and self.kpath_counts[p] <= self.rare_threshold
        }
        rare_kpath_example = None                    # [PROMPT]
        for _inp, _kpset in accepted_samples:
            if _kpset & rare_kpaths:
                rare_kpath_example = _inp
                break
        # Map each newly discovered k-path to an accepted input
        # that actually contains that k-path.
        new_kpath_examples = {}
        for _inp, _kpset in accepted_samples:
            for _kp in (_kpset & new_kpaths_this_round):
                new_kpath_examples.setdefault(_kp, _inp)

        # rejection rates + trends + new-context detection
        reject_signals = []
        for cat, cnt in reject_counter.items():
            rate = cnt / total if total else 0.0
            trend = self._trend(self.prev_reject_rates.get(cat), rate)
            new_error = cat not in self.seen_reject_categories
            new_contexts = reject_contexts[cat] - self.reject_context_history[cat]
            reject_signals.append({
                "category": cat, "count": cnt, "rate": rate, "trend": trend,
                "new_error": new_error, "new_contexts": sorted(new_contexts),
                "example": reject_examples.get(cat),   # [PROMPT]
            })
            self.seen_reject_categories.add(cat)
            self.reject_context_history[cat] |= reject_contexts[cat]
            self.prev_reject_rates[cat] = rate

        # [FIX] validity_rate excludes crashes from the denominator, so a
        # crash-heavy round (the goal) never trips the "raise validity" guardrail.
        non_crash = accepted + rejected
        obs = {
            "round": self.round_index,
            "total": total,
            "accepted": accepted, "rejected": rejected, "crashed": crashed,
            "acceptance_rate": accepted / total if total else 0.0,     # overall (reporting)
            "validity_rate": accepted / non_crash if non_crash else 1.0,  # guardrail basis
            "new_kpath_count": len(new_kpaths_this_round),
            "new_kpaths": sorted(new_kpaths_this_round)[:50],
            "rare_kpath_count": len(rare_kpaths),
            "rare_kpaths": sorted(rare_kpaths)[:50],
            "kpath_round_counts": dict(kpath_round_counts),
            "new_kpath_example": new_kpath_example,      # [PROMPT]
            "new_kpath_examples": new_kpath_examples,
            "rare_kpath_example": rare_kpath_example,    # [PROMPT]
            "reject_signals": reject_signals,
            "crash_signals": crash_candidates,
        }
        self.history.append(obs)
        return obs

    # ---- helpers ---------------------------------------------------------
    @staticmethod
    def _trend(prev, cur):
        if prev is None:
            return "NEW"
        if cur > prev + 1e-9:
            return "INCREASING"
        if cur < prev - 1e-9:
            return "DECREASING"
        return "STABLE"

    @staticmethod
    def _reject_context(text):
        fp = structural_fingerprint(text)
        bits = []
        if fp["inline_tables"]: bits.append("inline_table")
        if fp["arrays"]:        bits.append("array")
        if fp["array_tables"]:  bits.append("array_table")
        if fp["dotted_keys"]:   bits.append("dotted_key")
        if fp["max_depth"] >= 5: bits.append(f"depth>={min(fp['max_depth'], 30)}")
        return ">".join(bits) if bits else "flat"

    # ======================================================================
    # 3. The two design methods (author's proxy-signal steering logic).
    # ======================================================================
    def select_signals(self, obs, max_signals=DEFAULT_FEEDBACK_BUDGET):
        """Cross-source selection under an equal signal budget.
        Priority: NEW_CRASH > NEW_ACCEPTED_KPATH > NEW_ERROR > NEW_ERROR_CONTEXT
        > INCREASING_ERROR > RARE_ACCEPTED_STRUCTURE > FREQUENT_ERROR > KNOWN_CRASH.
        If validity falls below the floor, one slot is reserved for LOW_ACCEPTANCE."""
        if max_signals <= 0:
            return []

        candidates = []
        validity_rate = obs.get("validity_rate", 1.0)   # [FIX] use validity, not acceptance
        acceptance_floor = 0.20
        guardrail = None

        if obs.get("total", 0) > 0 and validity_rate < acceptance_floor:
            guardrail = {
                "source": "acceptance", "type": "LOW_ACCEPTANCE", "priority": 0,
                "validity_rate": round(validity_rate, 3), "floor": acceptance_floor,
                "action": "increase_valid_toml_generation",
            }

        for crash in obs.get("crash_signals", []):
            kind = "NEW_CRASH" if crash.get("new") else "KNOWN_CRASH"
            candidates.append({
                "source": "crash", "type": kind,
                "priority": 1 if crash.get("new") else 8,
                "signature": crash.get("signature"), "termination": crash.get("termination"),
                "bug_type": crash.get("bug_type"), "signal": crash.get("signal"),
                "similar": crash.get("similar", []),
            })

        if obs.get("new_kpath_count", 0) > 0:
            candidates.append({
                "source": "acceptance",
                "type": "NEW_ACCEPTED_KPATH",
                "priority": 2,
                "count": obs["new_kpath_count"],
                "examples": self._rank_kpaths_for_llm(
                    obs.get("new_kpaths", []),
                    limit=3,
                ),
            })

        # [FIX] RARE now driven by genuinely rare (seldom-seen) k-paths.
        if obs.get("rare_kpath_count", 0) > 0 and obs.get("accepted", 0) > 0:
            candidates.append({
                "source": "acceptance", "type": "RARE_ACCEPTED_STRUCTURE", "priority": 6,
                "count": obs["rare_kpath_count"], "examples": obs.get("rare_kpaths", [])[:3],
            })

        for rej in obs.get("reject_signals", []):
            contexts = rej.get("new_contexts") or []
            trend = rej.get("trend")
            if rej.get("new_error"):
                priority, kind = 3, "NEW_ERROR"
            elif contexts:
                priority, kind = 4, "NEW_ERROR_CONTEXT"
            elif trend == "INCREASING":
                priority, kind = 5, "INCREASING_ERROR"
            elif rej.get("rate", 0.0) > 0.10:
                priority, kind = 7, "FREQUENT_ERROR"
            else:
                continue
            candidates.append({
                "source": "rejection", "type": kind, "priority": priority,
                "error": rej.get("category"), "rate": round(rej.get("rate", 0.0), 3),
                "trend": trend, "contexts": contexts[:2], "frequency": rej.get("count", 0),
            })

        def sort_key(c):
            magnitude = 0
            if c["type"] == "NEW_CRASH":               magnitude = 100
            elif c["type"] == "NEW_ACCEPTED_KPATH":    magnitude = c.get("count", 0)
            elif c["type"] == "NEW_ERROR":             magnitude = 50
            elif c["type"] == "NEW_ERROR_CONTEXT":     magnitude = len(c.get("contexts", []))
            elif c["type"] == "INCREASING_ERROR":      magnitude = c.get("rate", 0.0)
            elif c["type"] == "RARE_ACCEPTED_STRUCTURE": magnitude = c.get("count", 0)
            elif c["type"] == "FREQUENT_ERROR":        magnitude = c.get("frequency", 0)
            return (c["priority"], -magnitude, c.get("source", ""), c.get("type", ""),
                    c.get("error", ""), c.get("signature") or "")

        candidates.sort(key=sort_key)
        selected = [guardrail] if guardrail is not None else []

        for candidate in candidates:
            if len(selected) >= max_signals:
                break
            key = (candidate.get("source"), candidate.get("type"),
                   candidate.get("error"), candidate.get("signature"))
            if any((s.get("source"), s.get("type"), s.get("error"), s.get("signature")) == key
                   for s in selected):
                continue
            selected.append(candidate)

        return selected[:max_signals]

    def frequency_baseline(self, obs, max_signals=DEFAULT_FEEDBACK_BUDGET,
                           use_guardrail=False):
        """Unified frequency-only baseline for fair comparison.

        Uses the same candidate sources as the adaptive selector:
        crash signatures, accepted k-paths, and rejection categories.

        It deliberately ignores:
          - novelty
          - structural context
          - trend
          - rarity
          - acceptance guardrail

        It only asks:
            "Which observable signal occurred most often this round?"

        The normal campaign budget is DEFAULT_FEEDBACK_BUDGET (2). The max_signals argument remains configurable so controlled experiments can use the same budget for both selectors.
        """
        if max_signals <= 0:
            return []

        candidates = []

        # Crash frequency: count repeated signatures in this round.
        # Keep a representative crash per signature so the baseline can be
        # verbalized with the SAME richness as the adaptive path (fair test).
        crash_by_sig = {}
        for c in obs.get("crash_signals", []):
            sig = c.get("signature")
            if sig and sig not in crash_by_sig:
                crash_by_sig[sig] = c
        crash_counts = Counter(
            c.get("signature")
            for c in obs.get("crash_signals", [])
            if c.get("signature")
        )
        for signature, count in crash_counts.items():
            rep = crash_by_sig.get(signature, {})
            candidates.append({
                "source": "crash",
                "type": "FREQUENT_CRASH",
                "frequency": count,
                "signature": signature,
                "bug_type": rep.get("bug_type"),
                "signal": rep.get("signal"),
            })

        # Accepted-structure frequency: current-round k-path frequency.
        for path, count in obs.get("kpath_round_counts", {}).items():
            candidates.append({
                "source": "acceptance",
                "type": "FREQUENT_ACCEPTED_KPATH",
                "frequency": count,
                "kpath": path,
            })

        # Rejection frequency: current-round category frequency.
        for rej in obs.get("reject_signals", []):
            candidates.append({
                "source": "rejection",
                "type": "FREQUENT_ERROR",
                "frequency": rej.get("count", 0),
                "error": rej.get("category"),
            })

        # Rank WITHIN each source by frequency (deterministic ties), then fill
        # the budget round-robin ACROSS sources. This keeps the baseline purely
        # frequency-driven while preventing a single high-cardinality source
        # (k-paths) from swamping the budget — so it stays a FAIR control that
        # differs from the adaptive selector only in the ranking criterion, not
        # in which sources it can surface.
        def tie(c):
            return (-c["frequency"], c.get("error", ""),
                    c.get("signature") or "", str(c.get("kpath", "")))
        by_source = {"crash": [], "acceptance": [], "rejection": []}
        for c in candidates:
            by_source.setdefault(c.get("source"), []).append(c)
        for s in by_source:
            by_source[s].sort(key=tie)

        # Optional guardrail: OFF by default (pure-frequency baseline). Turn ON
        # to run the "isolate the selection criterion" experiment, where both
        # selectors share the guardrail and differ ONLY in ranking.
        selected = []
        if (use_guardrail and obs.get("total", 0) > 0
                and obs.get("validity_rate", 1.0) < 0.20):
            selected.append({
                "source": "acceptance", "type": "LOW_ACCEPTANCE", "priority": 0,
                "validity_rate": round(obs.get("validity_rate", 1.0), 3),
                "floor": 0.20, "action": "increase_valid_toml_generation",
            })

        order = ["crash", "acceptance", "rejection"]
        cursor = {s: 0 for s in order}
        while (len(selected) < max_signals
               and any(cursor[s] < len(by_source[s]) for s in order)):
            for s in order:
                if len(selected) >= max_signals:
                    break
                if cursor[s] < len(by_source[s]):
                    selected.append(by_source[s][cursor[s]])
                    cursor[s] += 1
        return selected[:max_signals]

    def build_llm_feedback(self, selected, obs):
        """Translate selected internal signals into concrete, actionable steering.
        Design: explicit action + human TOML concepts (not raw labels) + a SHORT
        concrete example input anchoring each signal (Fuzz4All-style example +
        strategy) + controlled-variation + validity-awareness. Selection is
        unchanged; this is the communication layer only."""
        reject_by_cat = {r.get("category"): r for r in obs.get("reject_signals", [])}
        crash_by_sig = {c.get("signature"): c for c in obs.get("crash_signals", [])}
        instructions = []

        low_validity_mode = any(
        s.get("type") == "LOW_ACCEPTANCE"
        for s in selected
        )

        for signal in selected:
            kind = signal.get("type")

            if kind == "LOW_ACCEPTANCE":
                instructions.append(
                    "Validity is currently low. Rebalance generation toward VALID TOML "
                    "so that structural exploration remains effective, but continue "
                    "targeted malformed-boundary exploration. Do not repeatedly reproduce "
                    "the same rejected value or malformed pattern. Instead, explore nearby "
                    "structures with controlled variation while maintaining both valid "
                    "grammar exploration and targeted parser-boundary exploration. "
                    "Preserve diversity across arrays, inline tables, dotted keys, "
                    "nesting, tables, and array tables. Treat validity as a constraint on "
                    "exploration, not as the sole objective."
                )

            elif kind in ("NEW_CRASH", "KNOWN_CRASH", "FREQUENT_CRASH"):
                c = crash_by_sig.get(signal.get("signature"), {})
                ex = _clip(c.get("example_input") or signal.get("example_input"))
                details = [x for x in (signal.get("signal"), signal.get("bug_type")) if x]
                label = ", ".join(details) if details else "abnormal behavior"
                lead = "A new crash was found" if kind == "NEW_CRASH" else "A crash region recurred"
                ex_txt = f" Example input: `{ex}`." if ex else ""
                instructions.append(
                    f"{lead} ({label}).{ex_txt} Produce controlled VARIATIONS around "
                    "this input (change values, nesting depth, surrounding keys) "
                    "rather than repeating it verbatim.")

            elif kind == "NEW_ACCEPTED_KPATH":
                examples_by_kpath = obs.get("new_kpath_examples", {})

                selected_kpaths = signal.get("examples", [])

                concepts = _kpath_concepts(selected_kpaths, max_n=3)

                matched_examples = []
                for kp in selected_kpaths:
                    ex = examples_by_kpath.get(tuple(kp))
                    if ex and ex not in matched_examples:
                        matched_examples.append(ex)

                focus = f" Focus on structure like: {concepts}." if concepts else ""

                if matched_examples:
                    example_text = " ".join(
                        f"Example: `{_clip(ex)}`."
                        for ex in matched_examples[:2]
                    )
                else:
                    example_text = ""

                instructions.append(
                    "Generate more VALID TOML that extends recently reached grammar "
                    "structure with controlled variation (new combinations, deeper "
                    f"nesting), not exact duplicates.{focus} {example_text}"
                )

            elif kind == "RARE_ACCEPTED_STRUCTURE":
                ex = _clip(obs.get("rare_kpath_example"))
                ex_txt = f" Example: `{ex}`." if ex else ""
                instructions.append(
                    "Generate more VALID TOML around a rarely-explored valid structure; "
                    f"avoid reverting to only the most common forms.{ex_txt}")

            elif kind in ("NEW_ERROR", "NEW_ERROR_CONTEXT", "INCREASING_ERROR", "FREQUENT_ERROR"):
                cat = signal.get("error")
                r = reject_by_cat.get(cat, {})
                ex = _clip(r.get("example"))

                if kind == "NEW_ERROR":
                    lead = f"A new parser-rejection category ({cat}) appeared."
                elif kind == "NEW_ERROR_CONTEXT":
                    ctx = ", ".join(signal.get("contexts", []))
                    lead = (
                        f"Rejection {cat} appeared in a new context ({ctx})."
                        if ctx
                        else f"Rejection {cat} appeared in a new structural context."
                    )
                elif kind == "INCREASING_ERROR":
                    lead = f"Rejection {cat} is becoming more frequent."
                else:
                    lead = f"Rejection {cat} is common."

                ex_txt = f" Example that triggered it: `{ex}`." if ex else ""

                if low_validity_mode:
                    instructions.append(
                        f"{lead}{ex_txt} Treat this rejection as boundary evidence. Do not "
                        "repeatedly reproduce the exact rejected value or malformed pattern. "
                        "Explore nearby boundary cases through small, deliberate variations "
                        "while preserving a substantial stream of valid TOML and structural "
                        "diversity. Use the rejection to guide exploration of the surrounding "
                        "parser boundary rather than turning the generator into repeated "
                        "copies of the same invalid case."
                    )
                    
                else:
                    instructions.append(
                        f"{lead}{ex_txt} Probe this boundary: generate NEAR-VALID TOML "
                        "variants that stay close to valid but vary the structure, rather "
                        "than repeating the same malformed pattern."
                    )

        return {
            "round": obs.get("round"),
            "acceptance_rate": round(obs.get("acceptance_rate", 0.0), 3),
            "validity_rate": round(obs.get("validity_rate", 1.0), 3),
            "selected_signals": selected,
            "instructions": instructions,
        }

# ==========================================================================
# 4. Self-test (exercises the full path: aggregate -> select -> build).
# ==========================================================================
if __name__ == "__main__":
    agg = FeedbackAggregator(kpath_k=2)
    r1 = [
        {"input": "x = 1", "report": {"termination": "NORMAL"}},
        {"input": "y = [1,2]", "report": {"termination": "NORMAL"}},
        {"input": "z = 1e9_99", "report": {"termination": "REJECT",
            "rejection": {"error_type": "FLOAT_PARSE"}}},
    ]
    r2 = [
        {"input": "a = {b={c=1}}", "report": {"termination": "NORMAL"}},
        {"input": "w = [3,4]", "report": {"termination": "NORMAL"}},   # recurs array -> rare test
        {"input": "d = 1", "report": {"termination": "REJECT",
            "rejection": {"error_type": "DUPLICATE_KEY"}}},
    ]
    o1 = agg.add_round(r1)
    o2 = agg.add_round(r2)
    print("round2:", {k: o2[k] for k in
          ("acceptance_rate", "validity_rate", "new_kpath_count", "rare_kpath_count")})
    selected = agg.select_signals(o2, max_signals=3)
    print("\nselected:")
    for s in selected:
        print("   ", s["type"], "| priority", s["priority"])
    print("\ninstructions:")
    for line in agg.build_llm_feedback(selected, o2)["instructions"]:
        print("   -", line)
    print("\nAdaptive feedback self-test OK.")
