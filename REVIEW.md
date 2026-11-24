# Bosnia API integration: private review draft (revised)

24 November 2026. Prepared against repository commit e908242276441db9e442131ff4ec3c9533846e21.

Nothing in GitHub, its secrets, the website or the Excel workbook was changed. This is a proposed patch, not an activated pipeline. It contains no API token and no real API response data.

## Corrections made in this revision

1. **Bosnia EIC code fixed.** The patch used `10YBA-JPCC-----D`. The base `update_bih.py` uses `10YBA-JPCC-----B`, and the value now in `entsoe_adapter.py` is `10YBA-JPCC-----B`. This is an inference from the base file and from the EIC list convention. It must be confirmed against the official area list before rollout, because the sandbox used to prepare this could not reach it. A literal-value test (`AreaCodeTests`) was added because the existing tests read `AREAS` and would not catch a wrong constant.

## Proposed changes

- `entsoe_adapter.py`: Bosnia day-ahead load forecast (A65/A01), six physical-flow directions with Croatia, Serbia and Montenegro (A11), and both Croatia day-ahead scheduled-exchange directions (A09, contract A01).
- Three separate runtime CSVs: `entsoe_load_forecast.csv`, `entsoe_physical_flows.csv`, `entsoe_hr_schedules.csv`. None is in the repository yet, and none appears in Excel.
- UTC interval start/end as keys; offset-aware Sarajevo display times. Repeated DST hours are distinguished by UTC key.
- A03 repeated-value blocks expand only within their own Period. Validation covers document type, process type, areas, direction, schedule contract, units, finite nonnegative values, duplicate/overlapping/gapped intervals and full-day coverage. Multiple series are rejected rather than summed or chosen.
- Any failing direction keeps the dataset's previous CSV unchanged. Writes are atomic and idempotent.
- Token read only from `ENTSOE_API_TOKEN`, sent in a request header, never in the URL. Redirects are refused. HTTP 401, 403 and 429 stop further requests in that run. Errors record only the exception class name.
- Each run requests the previous complete Sarajevo day: nine calls. Up to 30 days of successful history accumulate. There is no automatic backfill. This is historical data, not intraday.
- `pipeline_status.py` separates run-finished time, per-dataset check time, last successful fetch and requested coverage. A successful fetch is not described as whole-master verification.
- `last_verified_date` is kept for VBA compatibility and is no longer advanced. A flag marks it as legacy.
- Master CSV columns, order and all rows are preserved, including IDs after BIH-0487. Values that depend on fallback or unverified sources are kept rather than relabelled as fresh.
- Balancing values are marked as typed-in August 2025 references. Mixed BHAS values are marked unverified. FX stays undated and is not republished as live. The positional DAM parser is disabled for publication.
- The workflow references `secrets.ENTSOE_API_TOKEN` by name only. No secret has been created.

## Intentional behaviour changes

1. Runs will stay red for undated FX, unverified BHAS and the disabled DAM publication, even when every ENTSO-E dataset passes. This is honest failure reporting, not proof of an API failure. Those adapters need repair before a fully green rollout.
2. The standalone XLSX generator is opt-in via `BUILD_LEGACY_XLSX=1`. Its narrative and labels are static and are not repaired here.
3. The failure email is gated behind repository variable `ENABLE_FAILURE_EMAIL=true`, in addition to SMTP configuration. Enable it only after reviewing the stricter failure states and recipients.
4. Existing consumers still read their old CSVs. No source substitution and no net-flow arithmetic is applied.
5. The `last_verified_date` field is now empty for runs that have not set it. The old VBA header reads that field and does not understand the new status fields. It is not a reliable live-freshness indicator.

## Cadence recommendation (not applied)

The workflow cron is still `*/30 * * * *`. At that cadence the nine ENTSO-E requests repeat 48 times a day, which is 432 calls for the same previous-day data. Recommended before rollout:

- Run the ENTSO-E step once a day, after the previous Sarajevo day is complete (for example 02:00 UTC), and keep the 30-minute schedule for the unrelated NOS and flows feeds. This needs a separate approval because it changes how the workflow runs.
- Optionally, a deliberate revision refresh of the most recent two or three days, not every run.

## Test evidence (run during this revision)

- `python3 -m unittest discover -s tests -v`: 36 tests, all passing (35 from the patch, plus `AreaCodeTests`). pytest was not installed.
- `python3 -m py_compile` on all changed Python files: passed.
- Workflow YAML parsed with PyYAML: passed.
- `git apply --check` against the base commit tree: passed. The full patch and the corrected files were both applied to a clean copy of the base and re-tested.
- Full 487-row, 30-column master comparison on the real `bih_data.csv`, with a synthetic BIH-0999 row added and all sources set to unverified: row IDs, column order and values preserved.

## Not run and not proven

- The nine earlier real XML samples are not in the uploaded files, so the 216-interval replay was **not repeated**. The earlier 216-interval figure is from the previous draft and is not verified here.
- No live ENTSO-E request, no authenticated transport test and no token use.
- No GitHub runner run and no workflow dispatch. The dependency versions are the local sandbox's, not a fresh runner install.
- No live DST day, no multi-month availability or revision study, no duplicate-document reconciliation, no independent NOS reconciliation.
- No Excel or VBA test. The existing master narratives and verification labels are not fully audited.
- Full legal review of reuse rights for each use context has not been done.
- The Bosnia EIC code is inferred and must be checked against the official list.

## Staged rollout (each stage needs separate approval)

1. Confirm the Bosnia EIC code and the other three area codes against the official list.
2. Agree the failure-reporting, alert and cadence choices above.
3. Create `ENTSOE_API_TOKEN` as a repository or environment secret, set by the owner. Do not paste it in chat or commit it.
4. Run the workflow manually on a branch with the ENTSO-E step only, and inspect the three CSVs and `status.json`.
5. Replay the earlier real samples, if they can be shared without the token or raw data concerns, and record the result.
6. Decide on publication to `main`.
7. VBA mapping of the new CSVs, then a copy-of-workbook test. Both are separate later work.

## Sources

- Repository: https://github.com/georgesmichael1/bih-energy-intelligence
- Official API: https://web-api.tp.entsoe.eu/api
- A03 specification: https://eepublicdownloads.entsoe.eu/clean-documents/EDI/Library/Introduction_of_different_time_series_v1.4.1.pdf
- Area codes: https://transparencyplatform.zendesk.com/hc/en-us/articles/15885757676308-Area-List-with-Energy-Identification-Code-EIC
- Request endpoint: https://transparencyplatform.zendesk.com/hc/en-us/articles/15696677194644-Request-Endpoint
