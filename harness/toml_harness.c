/*
 * toml_harness.c  —  fuzzing harness for tomlc17
 *
 * Reads a TOML document from a file (argv[1]) and parses it with tomlc17.
 * Adapted from the Phase-0 toy harness: same file-reading + malloc-check
 * skeleton, but the marker-word logic is replaced by a real toml_parse() call.
 *
 * EXIT-CODE DISCIPLINE (the "crash vs. rejection" judgment):
 *   0  = valid parse      : tomlc17 accepted the input (result.ok == true)
 *   1  = clean rejection  : tomlc17 refused malformed input (result.ok == false)
 *                           -> this is CORRECT behavior, NOT a bug
 *   2  = harness error    : bad usage / cannot open file / OOM (distinguishable)
 *
 * A memory-safety or undefined-behavior bug does NOT return through here:
 * the AddressSanitizer / UndefinedBehaviorSanitizer runtime aborts the process
 * and prints a report to stderr before control returns. That abort is what the
 * fuzzer detects as a crash. The harness contains no crash-detection code.
 */

#include <stdio.h>
#include <stdlib.h>
#include "tomlc17.h"

#define EXIT_VALID   0
#define EXIT_REJECT  1
#define EXIT_HARNESS 2

int main(int argc, char **argv) {
    if (argc != 2) {
        fprintf(stderr, "usage: %s <input.toml>\n", argv[0]);
        return EXIT_HARNESS;
    }

    /* ---- read the whole file into a heap buffer ---- */
    FILE *fp = fopen(argv[1], "rb");   /* "rb": no newline translation on Windows */
    if (fp == NULL) {
        fprintf(stderr, "harness: cannot open %s\n", argv[1]);
        return EXIT_HARNESS;
    }

    fseek(fp, 0, SEEK_END);
    long n = ftell(fp);
    fseek(fp, 0, SEEK_SET);
    if (n < 0) {
        fprintf(stderr, "harness: ftell failed\n");
        fclose(fp);
        return EXIT_HARNESS;
    }

    char *buf = malloc((size_t)n + 1);   /* +1 for the NUL terminator */
    if (buf == NULL) {
        fprintf(stderr, "harness: out of memory\n");
        fclose(fp);
        return EXIT_HARNESS;
    }

    size_t got = fread(buf, 1, (size_t)n, fp);
    fclose(fp);
    buf[got] = '\0';   /* tomlc17 REQUIRES a NUL-terminated src (see tomlc17.h) */

    /* ---- parse ----
     * toml_parse(src, len): len excludes the NUL. tomlc17 copies everything it
     * keeps into its own pool, so buf may be freed after parsing.
     * Alternative entry point: toml_parse_file_ex(argv[1]) also tests the
     * library's own file reading. We use the in-memory path here.
     */
    toml_result_t result = toml_parse(buf, (int)got);

    int rc;
    if (result.ok) {
        rc = EXIT_VALID;
    } else {
        /* Malformed input tomlc17 correctly refused. Surface the reason so the
         * fuzzer can log WHAT was rejected (useful proxy signal in Phase 4). */
        fprintf(stderr, "reject: %s\n", result.errmsg);
        rc = EXIT_REJECT;
    }

    /* ---- ALWAYS free, even on rejection ----
     * tomlc17 uses a pool allocator, and ASan bundles LeakSanitizer. Skipping
     * this on the reject path would make LSan report a leak, which the fuzzer
     * would misread as a crash. The API also requires toml_free() on every
     * result. Free the result first, then the input buffer.
     */
    toml_free(result);
    free(buf);

    return rc;
}
