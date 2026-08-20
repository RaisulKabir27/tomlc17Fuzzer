"""
malformed_tracker.py — Adaptive malformed exploration.

Tracks which parser rejection categories have been reached across campaigns
and ranks them so the refinement prompt can steer malformed generation
toward unexplored territory.

POLICY (student-authored): EXPLORATION.
    Categories that have NEVER appeared rank highest. A category the campaign
    has already reached repeatedly is demonstrated territory; a category never
    reached is a parser path the generator has never exercised at all, and
    unexercised code is where undiscovered bugs sit.

    The ranking below implements that policy. The policy itself, and its
    justification, belong in the report.

BLACK-BOX NOTE:
    Categories come from parser stderr — the target's own observable output,
    obtained by running it, not by instrumenting it. The inventory is the
    same taxonomy triage.py already uses to classify rejections. Step 4.4 of
    the assignment asks for "which grammar productions have and haven't
    appeared", and answering "haven't" requires an inventory of what could.
"""

from collections import Counter

from triage import _REJECT_RULES


# Full inventory of rejection categories the taxonomy can produce.
CATEGORY_INVENTORY = sorted({category for _, category in _REJECT_RULES})

# Categories that indicate a parser problem rather than ordinary rejection.
# Reaching one of these is a finding in its own right.

# A category counts as DORMANT if it was seen at some point but not within
# this many most-recent rounds. Tunable; 2 keeps a category "live" for one
# round after its last appearance before it re-enters the exploration front.
DORMANT_AFTER_ROUNDS = 2

# A seen category counts as RARE below this share of total rejections.
RARE_SHARE_THRESHOLD = 0.02


class MalformedTracker:
    """Accumulates per-category rejection history across campaigns."""

    def __init__(self, inventory=None):
        self.inventory = list(inventory or CATEGORY_INVENTORY)
        self.rounds = []          # list of Counter, one per campaign
        self.totals = Counter()   # cumulative counts
        self.first_seen = {}      # category -> round index (1-based)
        self.last_seen = {}       # category -> round index (1-based)

    # -- ingestion ---------------------------------------------------------
    def add_round(self, records):
        """Record one campaign's rejection categories. Returns that round's
        Counter."""
        counts = Counter()
        for rec in records:
            report = rec.get("report") or {}
            if report.get("termination") != "REJECT":
                continue
            category = (report.get("rejection") or {}).get("error_type")
            if category:
                counts[category] += 1

        self.rounds.append(counts)
        round_index = len(self.rounds)

        for category, n in counts.items():
            self.totals[category] += n
            self.first_seen.setdefault(category, round_index)
            self.last_seen[category] = round_index

        return counts

    # -- classification ----------------------------------------------------
    def classify(self, category):
        """Bucket a category by exploration status."""
        if category not in self.totals:
            return "NEVER_SEEN"

        rounds_elapsed = len(self.rounds) - self.last_seen[category]
        if rounds_elapsed >= DORMANT_AFTER_ROUNDS:
            return "DORMANT"

        total_rejections = sum(self.totals.values())
        if total_rejections:
            share = self.totals[category] / total_rejections
            if share < RARE_SHARE_THRESHOLD:
                return "RARE"

        return "FREQUENT"

    def ranked_categories(self):

        bucket_rank = {
            "NEVER_SEEN": 0,
            "DORMANT": 1,
            "RARE": 2,
            "FREQUENT": 3,
        }

        def sort_key(category):
            bucket = self.classify(category)
            return (
                bucket_rank[bucket],
                self.totals.get(category, 0),
                category,
            )

        return [
            (category, self.classify(category), self.totals.get(category, 0))
            for category in sorted(self.inventory, key=sort_key)
        ]

    def unexplored(self):
        return [c for c in self.inventory if c not in self.totals]

    def explored(self):
        return [c for c in self.inventory if c in self.totals]

    # -- communication -----------------------------------------------------
    def frontier(self, max_targets=6):
        ranked = self.ranked_categories()
        pool = [(c, b) for c, b, _ in ranked if b in ("NEVER_SEEN", "DORMANT")]

        if not pool:
            return []

        # Rotate through all unexplored/dormant categories.
        slots = min(max_targets, len(pool))
        offset = ((len(self.rounds) - 1) * slots) % len(pool)

        rotated = pool[offset:] + pool[:offset]
        return rotated[:slots]

    def build_instruction(self, max_targets=6):
        """Render the exploration frontier as a refinement instruction.

        Names this round's target categories and reports what has already
        been reached, so the model can tell demonstrated territory from
        untouched territory.
        """
        if not self.rounds:
            return ""

        targets = self.frontier(max_targets)

        if not targets:
            return (
                "MALFORMED EXPLORATION: every rejection category in the "
                "taxonomy has been reached recently. Vary the structural "
                "context in which malformed cases appear — nest them inside "
                "arrays, inline tables, and array-tables rather than "
                "emitting them at top level."
            )

        remaining = len(self.unexplored())
        lines = [
            "MALFORMED EXPLORATION (evidence from previous campaigns):",
            "",
            f"{remaining} of {len(self.inventory)} parser rejection "
            "categories have never been reached. Priority targets for this "
            "iteration:",
        ]
        for category, bucket in targets:
            note = " (reached earlier, not recently)" if bucket == "DORMANT" else ""
            lines.append(f"  - {category}{note}")

        seen = sorted(
            ((c, n) for c, n in self.totals.items()),
            key=lambda kv: -kv[1],
        )[:5]
        if seen:
            reached = ", ".join(f"{c} ({n})" for c, n in seen)
            lines += [
                "",
                f"Already reached repeatedly: {reached}. These are "
                "demonstrated territory — keep them represented, but do not "
                "spend additional malformed budget widening them.",
            ]

        lines += [
            "",
            "Add or strengthen malformed strategies that would plausibly "
            "provoke the unreached categories above. Keep every malformed "
            "case near-valid: a valid document with one deliberate change. "
            "Do not move these values into valid_leaf().",
        ]
        return "\n".join(lines)

    def final_report(self):
        """End-of-run summary of category coverage.

        The unreached list is a deliverable in its own right: it is the
        evidence-backed answer to "which parts of the grammar do you suspect
        are still under-tested". Categories are never retired, so anything
        listed here was targeted by the exploration policy and still never
        reached.
        """
        reached = sorted(
            self.explored(), key=lambda c: -self.totals[c]
        )
        unreached = sorted(self.unexplored())

        return {
            "rounds": len(self.rounds),
            "inventory_size": len(self.inventory),
            "reached_count": len(reached),
            "unreached_count": len(unreached),
            "coverage_fraction": round(
                len(reached) / len(self.inventory), 3
            ) if self.inventory else 0.0,
            "reached": [
                {
                    "category": c,
                    "total": self.totals[c],
                    "first_seen_round": self.first_seen[c],
                    "last_seen_round": self.last_seen[c],
                }
                for c in reached
            ],
            "unreached": unreached,
            "unreached_watch": [
            ],
        }

    def print_final_report(self):
        r = self.final_report()
        print(f"\nRejection-category coverage after {r['rounds']} campaigns: "
              f"{r['reached_count']}/{r['inventory_size']} "
              f"({r['coverage_fraction']:.1%})")

        if r["reached"]:
            print("\n  Reached:")
            for row in r["reached"]:
                print(f"    {row['category']:26} n={row['total']:<6} "
                      f"rounds {row['first_seen_round']}-{row['last_seen_round']}")

        if r["unreached"]:
            print(f"\n  Never reached ({r['unreached_count']}) — targeted by "
                  "the exploration policy throughout, still not triggered:")
            for c in r["unreached"]:
                print(f"    {c}")

    def summary(self):
        ranked = self.ranked_categories()
        return {
            "rounds": len(self.rounds),
            "categories_total": len(self.inventory),
            "categories_reached": len(self.explored()),
            "categories_unexplored": len(self.unexplored()),
            "coverage_fraction": round(
                len(self.explored()) / len(self.inventory), 3
            ) if self.inventory else 0.0,
            "per_round": [dict(r) for r in self.rounds],
            "totals": dict(self.totals),
            "ranked": [
                {"category": c, "status": b, "count": n}
                for c, b, n in ranked
            ],
        }


if __name__ == "__main__":
    # Replay the observed V1 and V2 campaigns.
    def fake(pairs):
        out = []
        for category, n in pairs:
            for _ in range(n):
                out.append({"report": {
                    "termination": "REJECT",
                    "rejection": {"error_type": category},
                }})
        return out

    tracker = MalformedTracker()

    tracker.add_round(fake([
        ("DUPLICATE_KEY", 120), ("FLOAT_PARSE", 90),
        ("INT_PARSE", 40), ("TABLE_CONFLICT", 99),
    ]))
    tracker.add_round(fake([
        ("DUPLICATE_KEY", 140), ("TABLE_CONFLICT", 100),
        ("ARRAY_TABLE_MISMATCH", 60), ("INLINE_TABLE_EXTENSION", 32),
    ]))

    s = tracker.summary()
    print(f"rounds: {s['rounds']}")
    print(f"reached {s['categories_reached']}/{s['categories_total']} "
          f"({s['coverage_fraction']:.1%})")
    print(f"unexplored: {s['categories_unexplored']}\n")

    print("Top of the exploration ranking:")
    for row in s["ranked"][:10]:
        print(f"  {row['status']:11} {row['category']:24} n={row['count']}")

    print("\n--- instruction sent to the LLM ---")
    print(tracker.build_instruction())
