from stack import compare_stacks


class CrashDatabase:
    def __init__(self):
        self.crashes = []

    def add_crash(self, report):
        self.crashes.append(report)

    def compare_with_existing(self, report):
        comparisons = []

        for existing in self.crashes:
            comparison = compare_stacks(
                report["stack"],
                existing["stack"]
            )

            comparisons.append({
                "existing_signature": existing.get("signature"),
                **comparison
            })

        return comparisons

if __name__ == "__main__":
    from stack import extract_stack, normalize_stack
    from runner import run_harness

    db = CrashDatabase()

    # Crash A
    result_a = run_harness("input_a.txt")
    frames_a = extract_stack(result_a["stderr"])
    stack_a = normalize_stack(frames_a)

    crash_a = {
        "signature": "CRASH_A",
        "stack": stack_a
    }

    # Store Crash A
    db.add_crash(crash_a)

    # Crash B
    result_b = run_harness("input_b.txt")
    frames_b = extract_stack(result_b["stderr"])
    stack_b = normalize_stack(frames_b)

    crash_b = {
        "signature": "CRASH_B",
        "stack": stack_b
    }

    # Compare B against existing crashes
    comparisons = db.compare_with_existing(crash_b)

    print("Crash A:", stack_a)
    print("Crash B:", stack_b)
    print("Comparisons:", comparisons)