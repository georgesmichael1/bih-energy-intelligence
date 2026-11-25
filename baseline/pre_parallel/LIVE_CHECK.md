# Live check of 3-worker ENTSO-E fetch (25 Nov 2026)

Run 45659398481 (manual, workers=3), job succeeded in 1m 48s: install 14 s, tests 11 s, previous-day fetch 57 s (3 workers), actual load/generation 18 s (unchanged sequential code), commit 2 s.
The earlier scheduled run 45605369867 (sequential, with backfill) took 7m 17s, fetch step 6m 06s. Different workloads, so not a like-for-like timing.

One-day temp benchmark (run 45658211879, same 25 requests each, temp folders): sequential 257.3 s, 3 workers 74.1 s (3.47x), all datasets OK, no errors, peak 23 requests/minute, ENTSO-E latency 4-18 s per request.

Output file review, committed CSVs before vs after the run, compared by key (dataset, areas, interval start):
- No rows lost; no values changed (Value_MW diffs: 0) in any file.
- Only Source_Document (new ENTSO-E document id) and Fetched_At_UTC changed, on exactly the refreshed day: 24 rows load_forecast, 48 hr_schedules, 144 ntc_day_ahead, 144 nominated_daily, 96 scheduled_rs_me, 144 scheduled_total, 24 actual_load.
- actual_generation: 42 new rows (current-day hours published since), 245 rows with new Source_Document/Fetched_At, 0 value diffs.
Scheduled default stays workers=1. Pre-parallel baseline: see README.md in this folder (commit 68b8614).
