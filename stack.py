
import re


def extract_stack(stderr):
    frames = []

    # Only process the primary crash stack.
    # Stop when ASan begins reporting the allocation stack.
    for line in stderr.splitlines():

        if "allocated by thread" in line:
            break

        match = re.search(
            r"#(\d+)\s+.*?\bin\s+([^\s]+).*?(?:\s+([A-Za-z]:[/\\].*?):(\d+):(\d+))?$",
            line
        )

        if match:
            frame_number = int(match.group(1))
            function = match.group(2)
            file_path = match.group(3)
            line_number = match.group(4)
            column = match.group(5)

            frames.append({
                "frame": frame_number,
                "function": function,
                "file": file_path,
                "line": int(line_number) if line_number else None,
                "column": int(column) if column else None,
            })

    return frames


def compare_stacks(stack_a, stack_b):
    similarity = lcs_similarity(stack_a, stack_b)

    if similarity == 1.0:
        relationship = "IDENTICAL_PATH"

    elif similarity == 0.0:
        relationship = "NO_OVERLAP"

    else:
        relationship = "PARTIAL_OVERLAP"

    return {
        "stack_similarity": similarity,
        "relationship": relationship,
    }


def normalize_stack(frames):
    normalized = []

    for frame in frames:
        function = frame["function"]

        # Ignore common Windows/runtime frames
        if function in {
            "__tmainCRTStartup",
            "WinMainCRTStartup",
            "mainCRTStartup",
        }:
            continue

        # Use function identity for structural comparison.
        normalized.append(function)

    return normalized


def lcs_sequence(stack_a, stack_b):
    m = len(stack_a)
    n = len(stack_b)

    dp = [[[] for _ in range(n + 1)] for _ in range(m + 1)]

    for i in range(1, m + 1):
        for j in range(1, n + 1):
            if stack_a[i - 1] == stack_b[j - 1]:
                dp[i][j] = dp[i - 1][j - 1] + [stack_a[i - 1]]
            else:
                if len(dp[i - 1][j]) >= len(dp[i][j - 1]):
                    dp[i][j] = dp[i - 1][j]
                else:
                    dp[i][j] = dp[i][j - 1]

    return dp[m][n]

def lcs_length(stack_a, stack_b):
    m = len(stack_a)
    n = len(stack_b)

    dp = [[0] * (n + 1) for _ in range(m + 1)]

    for i in range(1, m + 1):
        for j in range(1, n + 1):
            if stack_a[i - 1] == stack_b[j - 1]:
                dp[i][j] = dp[i - 1][j - 1] + 1
            else:
                dp[i][j] = max(
                    dp[i - 1][j],
                    dp[i][j - 1]
                )

    return dp[m][n]


def lcs_similarity(stack_a, stack_b):
    if not stack_a or not stack_b:
        return 0.0

    common = lcs_sequence(stack_a, stack_b)

    return len(common) / max(len(stack_a), len(stack_b))


if __name__ == "__main__":
    from runner import run_harness

    result_a = run_harness("input_a.txt")
    frames_a = extract_stack(result_a["stderr"])
    stack_a = normalize_stack(frames_a)

    result_b = run_harness("input_b.txt")
    frames_b = extract_stack(result_b["stderr"])
    stack_b = normalize_stack(frames_b)

    comparison = compare_stacks(stack_a, stack_b)

    print("Stack A:", stack_a)
    print("Stack B:", stack_b)
    print("LCS:", lcs_sequence(stack_a, stack_b))
    print("Comparison:", comparison)