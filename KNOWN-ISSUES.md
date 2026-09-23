# Compatibility status

This page records resolved regressions from the `af299e2` public snapshot, not a claim that every Windows configuration has been tested. For the current source version and release assets, see README.md and GitHub Releases. The test counts below are historical evidence from the fix, not a beta.6 full-suite result.

The two limits documented for the `af299e2` snapshot are addressed in this revision. Both have deterministic regression tests.

- **Uploads across volumes:** Uploads may start in system TEMP on a different Windows drive from task storage. Direct task creation now copies each file to a temporary path inside the task workspace, atomically replaces the destination on that volume, and cleans temporary uploads. `tests/test_web_jobs.py` simulates a cross-device rename failure.
- **Non-UTF-8 CLI output:** A dry-run previously wrote its SRT and then failed printing the Chinese profiler report under a legacy code page. Characters unsupported by stdout's encoding now use backslash escapes; UTF-8 output remains unchanged. `tests/test_cli_encoding.py` runs the CLI subprocess with `cp1252:strict`.

Windows CI now uses the runner's normal TEMP/TMP location. The suite uses UTF-8 for ordinary output, while the CLI subprocess regression explicitly exercises `cp1252:strict`. Local isolated validation: 1123 passed, 6 skipped; the skips require private data excluded from this repository. A physical D: to C: upload check passed with synthetic SRTs in D:/ProgramData; the original cross-volume rename reproduced WinError 17. Test files were removed.
