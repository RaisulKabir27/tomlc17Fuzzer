"""
budget.py — Token and cost accounting for the agentic loop.

The assignment caps spend at 5 iterations OR roughly $5 of LLM API spend,
whichever comes first, and requires both the iteration count and a
tokens/cost estimate in the report.

Only LLM generation calls consume this budget. The harness campaign, the
validation smoke test, and crash shrinking are all local — they cost wall
clock, not dollars.

Affordability is checked BEFORE each call, not after, so the loop never
discovers it is over budget having already spent the money.
"""

import json
from pathlib import Path


# ---------------------------------------------------------------------------
# PRICING — YOU MUST SET THESE.
#
# Quoted per 1,000,000 tokens, which is how Google quotes them.
# Look up the current rate for your exact model at:
#     https://ai.google.dev/pricing
#
# Thought tokens: on Gemini thinking models these are normally billed at the
# OUTPUT rate. If your model's pricing page says otherwise, adjust. With
# thinking_level="high" they can be a large share of the bill, so do not
# leave this at zero.
# ---------------------------------------------------------------------------
PRICE_PER_1M_INPUT = 0.25
PRICE_PER_1M_OUTPUT = 1.50
PRICE_PER_1M_THOUGHT = 0.0

# Conservative guess at what one generation call costs, used only for the
# pre-call affordability check when no call has completed yet. Replaced by
# the observed running average as soon as there is data.
INITIAL_CALL_COST_ESTIMATE_USD = 0.05


class BudgetExceeded(Exception):
    """Raised when a call cannot be afforded within the remaining budget."""


class BudgetTracker:
    """Tracks cumulative tokens and cost against the assignment's cap."""

    def __init__(self, max_cost_usd=5.0, max_iterations=5):
        self.max_cost_usd = max_cost_usd
        self.max_iterations = max_iterations

        self.input_tokens = 0
        self.output_tokens = 0
        self.thought_tokens = 0
        self.total_tokens = 0
        self.cost_usd = 0.0

        self.calls = []          # one entry per LLM call
        self.iterations_used = 0  # V1..V5 completed, not retries

    # -- pricing -----------------------------------------------------------
    @staticmethod
    def pricing_is_configured():
        return any((
            PRICE_PER_1M_INPUT,
            PRICE_PER_1M_OUTPUT,
            PRICE_PER_1M_THOUGHT,
        ))

    @staticmethod
    def price(usage):
        """Cost in USD for one call's usage dict."""
        inp = usage.get("input_tokens") or 0
        out = usage.get("output_tokens") or 0
        tho = usage.get("thought_tokens") or 0
        return (
            inp * PRICE_PER_1M_INPUT
            + out * PRICE_PER_1M_OUTPUT
            + tho * PRICE_PER_1M_THOUGHT
        ) / 1_000_000

    # -- recording ---------------------------------------------------------
    def record(self, usage, label):
        """Record a completed LLM call. Returns its cost in USD."""
        usage = usage or {}
        inp = usage.get("input_tokens") or 0
        out = usage.get("output_tokens") or 0
        tho = usage.get("thought_tokens") or 0
        total = usage.get("total_tokens") or (inp + out + tho)

        cost = self.price(usage)

        self.input_tokens += inp
        self.output_tokens += out
        self.thought_tokens += tho
        self.total_tokens += total
        self.cost_usd += cost

        self.calls.append({
            "label": label,
            "input_tokens": inp,
            "output_tokens": out,
            "thought_tokens": tho,
            "total_tokens": total,
            "cost_usd": round(cost, 6),
            "cumulative_cost_usd": round(self.cost_usd, 6),
        })
        return cost

    # -- checks ------------------------------------------------------------
    def average_call_cost(self):
        """Mean observed cost per call, or the initial estimate if no data."""
        if not self.calls:
            return INITIAL_CALL_COST_ESTIMATE_USD
        return self.cost_usd / len(self.calls)

    def remaining_usd(self):
        return max(0.0, self.max_cost_usd - self.cost_usd)

    def can_afford_call(self):
        """Whether one more generation call fits in the remaining budget.

        Uses the running average as the estimate. With pricing unconfigured
        every call prices at $0, so this always passes — that is why
        pricing_is_configured() should be checked at startup.
        """
        return self.average_call_cost() <= self.remaining_usd()

    def assert_can_afford(self, label=""):
        if not self.can_afford_call():
            raise BudgetExceeded(
                f"Cannot afford {label or 'next call'}: "
                f"estimated ${self.average_call_cost():.4f}, "
                f"remaining ${self.remaining_usd():.4f} "
                f"of ${self.max_cost_usd:.2f}."
            )

    def exhausted(self):
        return self.cost_usd >= self.max_cost_usd

    # -- reporting ---------------------------------------------------------
    def summary(self):
        return {
            "iterations_used": self.iterations_used,
            "max_iterations": self.max_iterations,
            "llm_calls": len(self.calls),
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "thought_tokens": self.thought_tokens,
            "total_tokens": self.total_tokens,
            "cost_usd": round(self.cost_usd, 6),
            "max_cost_usd": self.max_cost_usd,
            "remaining_usd": round(self.remaining_usd(), 6),
            "pricing_configured": self.pricing_is_configured(),
            "calls": self.calls,
        }

    def print_status(self):
        print(
            f"  [budget] calls={len(self.calls)}  "
            f"tokens={self.total_tokens}  "
            f"spent=${self.cost_usd:.4f}  "
            f"remaining=${self.remaining_usd():.4f}",
            flush=True,
        )

    def save(self, path):
        path = Path(path)
        path.parent.mkdir(exist_ok=True, parents=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.summary(), f, indent=2)


if __name__ == "__main__":
    b = BudgetTracker(max_cost_usd=5.0)

    print("Pricing configured:", b.pricing_is_configured())
    if not b.pricing_is_configured():
        print("WARNING: all prices are 0.0 — cost tracking is inactive.")
        print("Set PRICE_PER_1M_* in budget.py from https://ai.google.dev/pricing")

    # Demo with illustrative rates so the arithmetic is visible.
    globals()["PRICE_PER_1M_INPUT"] = 0.10
    globals()["PRICE_PER_1M_OUTPUT"] = 0.40
    globals()["PRICE_PER_1M_THOUGHT"] = 0.40

    demo = BudgetTracker(max_cost_usd=5.0)
    for i in range(1, 4):
        cost = demo.record(
            {
                "input_tokens": 6000,
                "output_tokens": 4000,
                "thought_tokens": 8000,
                "total_tokens": 18000,
            },
            label=f"generate_v{i}",
        )
        print(f"call {i}: ${cost:.6f}")

    demo.print_status()
    print("can afford another call:", demo.can_afford_call())
