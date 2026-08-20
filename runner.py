import os
import signal
import subprocess

# point at the real tomlc17 harness
HARNESS = os.environ.get("HARNESS", "harness/toml_harness.exe")


# [NEW] Identify the specific fatal signal behind an abnormal exit.
# POSIX: a process killed by signal N returns exit_code == -N.
# Windows (MSYS2/clang): a fatal fault returns a large positive exception code
# (e.g. 0xC0000005), NOT a negative signal number - so we map both conventions.
# Returns a name like 'SIGSEGV' / 'SIGABRT' / 'SIGFPE', or None if not a signal.
_WIN_EXCEPTIONS = {
    0xC0000005: "SIGSEGV (ACCESS_VIOLATION)",
    0xC000001D: "SIGILL (ILLEGAL_INSTRUCTION)",
    0xC0000094: "SIGFPE (INT_DIVIDE_BY_ZERO)",
    0xC0000095: "SIGFPE (INT_OVERFLOW)",
    0xC00000FD: "SIGSEGV (STACK_OVERFLOW)",
    0x80000003: "SIGABRT (BREAKPOINT)",
    3:          "SIGABRT (abort)",   # common CRT abort() exit code on Windows
}


def identify_signal(exit_code):
    if exit_code is None:
        return None
    # POSIX signal death
    if exit_code < 0:
        sig_num = -exit_code

        signal_map = {
            signal.SIGSEGV: "SIGSEGV",
            signal.SIGABRT: "SIGABRT",
            signal.SIGFPE: "SIGFPE",
        }

        if sig_num in signal_map:
            return signal_map[sig_num]

        try:
            return signal.Signals(sig_num).name
        except ValueError:
            return f"SIGNAL_{sig_num}"
    # Windows exception code
    if exit_code in _WIN_EXCEPTIONS:
        return _WIN_EXCEPTIONS[exit_code]
    if exit_code >= 0xC0000000:
        return f"WIN_EXCEPTION_{exit_code:#010x}"
    return None


def classify_result(exit_code, stdout, stderr, timed_out, sanitizers):
    if timed_out:
        return "TIMEOUT"

    if sanitizers:
        return "SANITIZER"

    if exit_code == 0:
        return "VALID"

    if exit_code == 1:
        return "REJECT"

    # the tomlc17 harness returns 2 for harness-level errors
    # (bad usage / cannot open file / OOM). This is NOT a crash.
    if exit_code == 2:
        return "HARNESS_ERROR"

    # Anything else is abnormal termination and counts as a crash:
    #   - POSIX: negative exit_code (killed by signal)
    #   - Windows: large positive exception code (e.g. 0xC0000005)
    #   - any other unexpected nonzero code
    # ABNORMAL (not SIGNAL) keeps it in the crash set feedback.py recognizes:
    # {SANITIZER, TIMEOUT, ABNORMAL}. The specific signal is reported separately
    # via the "signal" field, without changing this bucket.
    return "ABNORMAL"


def _detect_sanitizers(stderr):
    sanitizers = []
    if "AddressSanitizer" in stderr:
        sanitizers.append("ASAN")
    if "UndefinedBehaviorSanitizer" in stderr:
        sanitizers.append("UBSAN")
    # 'runtime error:' is UBSan's core diagnostic and the assignment's third
    # required signature. A bare UBSan hit prints this WITHOUT the header.
    if "runtime error:" in stderr and "UBSAN" not in sanitizers:
        sanitizers.append("UBSAN")
    return sanitizers


def run_harness(input_file):
    # make the sanitizers abort on first error so a bug becomes a clean process
    # death (and, on Windows, a detectable one) rather than a survived warning.
    env = dict(os.environ)
    env.setdefault("ASAN_OPTIONS", "abort_on_error=1:halt_on_error=1")
    env.setdefault("UBSAN_OPTIONS",
                   "abort_on_error=1:halt_on_error=1:print_stacktrace=1")

    try:
        result = subprocess.run(
            [HARNESS, input_file],
            capture_output=True,
            text=True,
            timeout=5,
            env=env,
        )

        sanitizers = _detect_sanitizers(result.stderr)

        status = classify_result(
            result.returncode,
            result.stdout,
            result.stderr,
            False,
            sanitizers
        )

        # [NEW] name the fatal signal, if this death was one. This is EXTRA
        # detail for triage/reporting; it does not change the status bucket.
        sig = identify_signal(result.returncode) if status == "ABNORMAL" else None

        return {
            "status": status,
            "exit_code": result.returncode,
            "signal": sig,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "timed_out": False,
            "sanitizers": sanitizers,
        }

    except subprocess.TimeoutExpired as e:
        stdout = e.stdout.decode() if isinstance(e.stdout, bytes) else (e.stdout or "")
        stderr = e.stderr.decode() if isinstance(e.stderr, bytes) else (e.stderr or "")
        return {
            "status": "TIMEOUT",
            "exit_code": None,
            "signal": None,
            "stdout": stdout,
            "stderr": stderr,
            "timed_out": True,
            "sanitizers": [],
        }


if __name__ == "__main__":
    result = run_harness("input.txt")
    print("Status:", result["status"])
    print("Exit code:", result["exit_code"])
    print("Signal:", result["signal"])
    print("Timed out:", result["timed_out"])
    print("Sanitizers:", result["sanitizers"])
    print("STDOUT:")
    print(result["stdout"])
    print("STDERR:")
    print(result["stderr"])
