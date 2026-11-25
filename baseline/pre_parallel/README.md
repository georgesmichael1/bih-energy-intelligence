# Pre-parallel ENTSO-E baseline (safe rollback reference)

These files are exact copies of the repository as of commit 68b8614 (the parent of the first parallel-fetch commit 5d741eb, 25 Nov 2026), saved before any speed optimisation:

- entsoe_adapter.py.txt  -> restore as entsoe_adapter.py
- test_entsoe.py.txt     -> restore as tests/test_entsoe.py
- entsoe_daily.yml.txt   -> restore as .github/workflows/entsoe_daily.yml

Rollback: either copy these three files back (data CSVs are never touched), or `git revert` only the optimisation commits 5d741eb, 28a13cc, bb9a2c1, c99f19d. Do not restore the whole repository; the data files change every 30 minutes.
The parallel code is opt-in: ENTSOE_WORKERS unset or 1 runs the original sequential path.
