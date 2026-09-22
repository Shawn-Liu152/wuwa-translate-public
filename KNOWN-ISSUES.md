# Known compatibility limits

The release CI uses UTF-8 output and keeps system temporary uploads and pytest data on the same workspace volume. Passing CI does not cover the following configurations.

- **Major — Uploads across volumes:** if the system TEMP directory and task storage are on different Windows drives, an SRT upload can fail with WinError 17. `web/app.py:1090` creates a system temporary file and `web/jobs.py:1564` uses `Path.replace`, which cannot move across volumes. Until fixed, run with TEMP/TMP on the task-storage volume. Recommended fix: destination-local staging with cleanup and atomic replacement; add a regression simulating a cross-device rename error.
- **Minor — Non-UTF-8 CLI output:** a dry-run can write its SRT but fail when printing a Chinese report under a legacy Windows code page. `pipeline/main.py:1412` prints the profiler report without ensuring a supported output encoding. Use PYTHONIOENCODING=utf-8 and PYTHONUTF8=1 for CLI runs. Add a subprocess regression using a legacy encoding before changing CLI output handling.

These were reproduced in Windows GitHub CI during publication. They remain unresolved product limitations; no translation, ASR, download or subtitle algorithms were changed to make CI pass. Cross-volume uploads are the next recommended fix.
