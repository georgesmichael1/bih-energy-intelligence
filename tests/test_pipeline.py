import datetime
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch
import pandas as pd
import requests as std_requests
import update_bih as u
import pipeline_status as ps


SAMPLE_NOS_HTML = """
<table id="productionTable">
  <tbody>
    <tr>
      <td>01:00</td>
      <td>1,200.5</td>
      <td>1,180.0</td>
      <td>950.0</td>
      <td>940.2</td>
    </tr>
    <tr>
      <td>00:00</td>
      <td>1,100.0</td>
      <td>1,090.0</td>
      <td>880.0</td>
      <td>870.0</td>
    </tr>
  </tbody>
</table>
"""


def _build_flows_html(ts_str: str = "24.11.2026. 12:30", net_summary: str = "-50") -> str:
    return f"""
    <html>
      <body>
        <div id="flow-time">{ts_str}</div>
        <div id="ace-value">4.5 MW</div>
        <svg id="map-flow-current">
          <style>
            #arrow-current-hr-01-out {{ display: inline; }}
            #arrow-current-sr-01-in {{ display: inline; }}
            #arrow-current-cg-01-in {{ display: inline; }}
          </style>
        </svg>
        <div id="current-value-hr">100</div>
        <div id="current-value-sr">80</div>
        <div id="current-value-cg">70</div>
        <div id="current-value-summary">{net_summary}</div>
        <svg id="map-flow-planned">
          <style>
            #arrow-planned-hr-01-out {{ display: inline; }}
            #arrow-planned-sr-01-in {{ display: inline; }}
            #arrow-planned-cg-01-in {{ display: inline; }}
          </style>
        </svg>
        <div id="planned-value-hr">100</div>
        <div id="planned-value-sr">80</div>
        <div id="planned-value-cg">70</div>
        <div id="planned-value-summary">{net_summary}</div>
      </body>
    </html>
    """


class PipelineTests(unittest.TestCase):
    def setUp(self):
        ps.DATASETS.clear()

    def test_master_keeps_all_rows_columns_and_values_when_sources_unverified(self):
        # Includes a hypothetical newer consumer row, not just the current 487.
        original = pd.DataFrame([
            {
                "Record_ID": key,
                "Primary_Value": "original",
                "Secondary_Value": "original",
                "Tariff_Price_Rate_Value": "original",
                "Extra_Consumer_Field": "keep",
            }
            for key in ["BIH-0092", "BIH-0128", "BIH-0487", "BIH-9999"]
        ])
        with tempfile.TemporaryDirectory() as tmp:
            csv = Path(tmp) / "bih_data.csv"
            original.to_csv(csv, index=False)
            status = Path(tmp) / "status.json"
            status.write_text('{"last_verified_date":"historic"}')
            u.sync_github_master_csv(
                None, {"Status": "FALLBACK"}, None, pd.DataFrame(columns=["Year"]), {}, csv, status
            )
            pd.testing.assert_frame_equal(pd.read_csv(csv), original)
            self.assertEqual(json.loads(status.read_text())["last_verified_date"], "historic")

    def test_main_smoke_all_network_mocked(self):
        fx = {
            "EUR_BAM_Middle": 1.95,
            "USD_BAM_Buy": 1.7,
            "USD_BAM_Middle": 1.8,
            "USD_BAM_Sell": 1.9,
            "Status": "FALLBACK",
        }
        with tempfile.TemporaryDirectory() as tmp:
            old = os.getcwd()
            os.chdir(tmp)
            try:
                pd.DataFrame([{"Record_ID": "BIH-0128", "Primary_Value": "old"}]).to_csv(
                    "bih_data.csv", index=False
                )
                with patch.object(u, "fetch_nosbih_balancing_prices", return_value=pd.DataFrame()), \
                     patch.object(u, "fetch_cbbh_fx_rates", return_value=fx), \
                     patch.object(u, "fetch_worldometer_bih_population", return_value=(1, pd.DataFrame(columns=["Year"]))), \
                     patch.object(u, "fetch_bhas_headline_kpis", return_value={}), \
                     patch.object(u, "fetch_worldbank_bih_macro", return_value=pd.DataFrame()), \
                     patch.object(u, "update_nos_history", side_effect=RuntimeError("mocked")), \
                     patch.object(u, "update_flows_history", side_effect=RuntimeError("mocked")), \
                     patch.object(u, "build_bih_intelligence_workbook") as workbook, \
                     patch.dict(os.environ, {"BUILD_LEGACY_XLSX": "0"}):
                    u.main()
                    workbook.assert_not_called()
                status = json.loads(Path("status.json").read_text())
                self.assertEqual(status["rows"], 1)
                self.assertNotIn("entsoe", status["dataset_details"])  # ENTSO-E now runs in entsoe_daily.yml
                self.assertFalse(list(Path(".").glob("*.xlsx")))
            finally:
                os.chdir(old)

    def test_default_requests_has_retry_adapter(self):
        session = u.create_resilient_session()
        self.assertIsInstance(session, u.std_requests.Session)
        self.assertEqual(session.get_adapter("https://").max_retries.total, 3)
        session.close()

    def test_fallback_fx_does_not_claim_today(self):
        with patch.object(u, "safe_get", side_effect=RuntimeError("offline")):
            rates = u.fetch_cbbh_fx_rates()
        self.assertIsNone(rates["Date"])
        self.assertEqual(rates["Status"], "FALLBACK")
        self.assertEqual(ps.DATASETS["fx"]["state"], "FALLBACK")

    def test_static_balancing_label(self):
        with patch.object(u, "safe_get", side_effect=RuntimeError("offline")):
            data = u.fetch_nosbih_balancing_prices()
        self.assertTrue(data.empty)
        self.assertEqual(ps.DATASETS["balancing"]["state"], "FAILED")

    def test_successful_population_uses_returned_values_only(self):
        ps.DATASETS.clear()
        ps.record("population", "OK")
        data = pd.DataFrame([
            {
                "Record_ID": "BIH-0092",
                "Primary_Value": "old",
                "Secondary_Value": "historical narrative",
                "Tariff_Price_Rate_Value": "old",
            }
        ])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "data.csv"
            data.to_csv(path, index=False)
            u.sync_github_master_csv(
                None,
                {},
                3000001,
                pd.DataFrame([{"Year": 2026, "WM_Population_Headcount": 3000002}]),
                {},
                path,
                Path(tmp) / "status.json",
            )
            result = pd.read_csv(path)
            self.assertIn("3,000,002", result.iloc[0]["Tariff_Price_Rate_Value"])
            self.assertEqual(result.iloc[0]["Secondary_Value"], "historical narrative")

    def test_nos_transport_defaults_to_cffi_with_no_nested_retries(self):
        with patch.object(u, "HAS_CFFI", True), \
             patch.dict(os.environ, {}, clear=True), \
             patch.object(u, "create_resilient_session") as factory:
            self.assertIs(u.create_nos_session(), factory.return_value)
            factory.assert_called_once_with(retries=0, backoff_factor=1.0, use_cffi=True)

    def test_nos_transport_explicit_requests_or_missing_cffi(self):
        for available, setting in [(True, "0"), (False, "1")]:
            with self.subTest(available=available, setting=setting), \
                 patch.object(u, "HAS_CFFI", available), \
                 patch.dict(os.environ, {"NOS_USE_CFFI": setting}), \
                 patch.object(u, "create_resilient_session") as factory:
                u.create_nos_session()
                factory.assert_called_once_with(retries=0, backoff_factor=1.0, use_cffi=False)

    def test_fetch_nos_day_success_and_enforces_tls_verify(self):
        session = MagicMock()
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"data": SAMPLE_NOS_HTML}
        session.post.return_value = resp

        day = datetime.date(2026, 11, 24)
        df = u.fetch_nos_day(session, day)

        self.assertEqual(len(df), 2)
        self.assertEqual(list(df["Hour"]), [1, 24])
        self.assertAlmostEqual(df.loc[df["Hour"] == 1, "Planned_Production_MW"].iloc[0], 1200.5)
        self.assertAlmostEqual(df.loc[df["Hour"] == 1, "Actual_Production_MW"].iloc[0], 1180.0)
        self.assertAlmostEqual(df.loc[df["Hour"] == 1, "Planned_Consumption_MW"].iloc[0], 950.0)
        self.assertAlmostEqual(df.loc[df["Hour"] == 1, "Actual_Consumption_MW"].iloc[0], 940.2)

        session.post.assert_called_once()
        _, kwargs = session.post.call_args
        self.assertTrue(kwargs.get("verify"))
        self.assertEqual(kwargs["data"]["action"], "production")
        self.assertEqual(kwargs["data"]["production"], "date=24.11.2026.")

    def test_fetch_nos_day_distinguishes_timeout_connection_http_and_parse_errors(self):
        day = datetime.date(2026, 11, 24)

        with patch.dict(os.environ, {"NOS_DISABLE_CFFI_FALLBACK": "1"}), \
             patch("time.sleep"):
            # 1. ReadTimeout -> NosTimeoutError (kind == "TIMEOUT")
            sess_timeout = MagicMock()
            sess_timeout.post.side_effect = std_requests.exceptions.ReadTimeout("read timed out")
            with self.assertRaises(u.NosTimeoutError) as ctx_t:
                u.fetch_nos_day(sess_timeout, day)
            self.assertEqual(ctx_t.exception.kind, "TIMEOUT")
            self.assertEqual(sess_timeout.post.call_count, u.NOS_MAX_ATTEMPTS)

            # 2. ConnectionError -> NosConnectionError (kind == "CONNECTION_ERROR")
            sess_conn = MagicMock()
            sess_conn.post.side_effect = std_requests.exceptions.ConnectionError("connection reset")
            with self.assertRaises(u.NosConnectionError) as ctx_c:
                u.fetch_nos_day(sess_conn, day)
            self.assertEqual(ctx_c.exception.kind, "CONNECTION_ERROR")

            # 3. HTTP 503 -> NosHttpError (kind == "HTTP_ERROR")
            sess_http = MagicMock()
            resp_503 = MagicMock()
            resp_503.status_code = 503
            sess_http.post.return_value = resp_503
            with self.assertRaises(u.NosHttpError) as ctx_h:
                u.fetch_nos_day(sess_http, day)
            self.assertEqual(ctx_h.exception.kind, "HTTP_ERROR")
            self.assertEqual(ctx_h.exception.status_code, 503)

            # 4. Malformed JSON / missing table -> NosParseError (kind == "PARSE_ERROR")
            sess_parse = MagicMock()
            resp_bad = MagicMock()
            resp_bad.status_code = 200
            resp_bad.json.return_value = {"data": "<div>no table here</div>"}
            sess_parse.post.return_value = resp_bad
            with self.assertRaises(u.NosParseError) as ctx_p:
                u.fetch_nos_day(sess_parse, day)
            self.assertEqual(ctx_p.exception.kind, "PARSE_ERROR")

    def test_update_nos_history_preserves_cache_and_does_not_rewrite_when_all_fail(self):
        today = datetime.date(2026, 11, 24)
        yesterday = today - datetime.timedelta(days=1)
        old_stamp = "2026-11-23T18:00+01:00"

        cached_rows = []
        for i in range(u.HISTORY_DAYS):
            d = today - datetime.timedelta(days=i)
            cached_rows.append({
                "Date": d.isoformat(),
                "Hour": 1,
                "Planned_Production_MW": 1000.0,
                "Actual_Production_MW": 950.0,
                "Planned_Consumption_MW": 800.0,
                "Actual_Consumption_MW": 780.0,
                "Fetched_At": old_stamp,
            })
        cached_df = pd.DataFrame(cached_rows)

        with tempfile.TemporaryDirectory() as tmp:
            old_cwd = os.getcwd()
            os.chdir(tmp)
            try:
                cached_df.to_csv(u.NOS_HOURLY_CSV, index=False)
                raw_before = Path(u.NOS_HOURLY_CSV).read_bytes()

                with patch.object(u, "_sarajevo_today", return_value=today), \
                     patch.object(u, "fetch_nos_day", side_effect=u.NosTimeoutError("Read timed out")), \
                     patch("time.sleep"), \
                     patch.object(pd.DataFrame, "to_csv", wraps=cached_df.to_csv) as spy_to_csv:
                    res = u.update_nos_history()
                    spy_to_csv.assert_not_called()

                raw_after = Path(u.NOS_HOURLY_CSV).read_bytes()
                self.assertEqual(raw_before, raw_after)
                self.assertEqual(res["nos_days_requested"], 2)  # today and yesterday refetch
                self.assertEqual(res["nos_days_succeeded"], 0)
                self.assertEqual(len(res["nos_days_failed"]), 2)
                self.assertEqual(res["nos_last_successful_fetch_at"], old_stamp)
                self.assertEqual(res["nos_fetched_at"], old_stamp)
                self.assertNotEqual(res["nos_last_attempt_at"], old_stamp)

                nos_meta = ps.DATASETS["nos"]
                self.assertEqual(nos_meta["state"], "FAILED")
                self.assertEqual(nos_meta["last_successful_fetch_at"], old_stamp)
                self.assertEqual(nos_meta["last_attempt_at"], res["nos_last_attempt_at"])
                self.assertEqual(nos_meta["error_counts"].get("TIMEOUT"), 2)

                # Verify write_status preserves prior last_success_at when state != OK
                status_path = Path(tmp) / "status.json"
                status_path.write_text(json.dumps({
                    "dataset_details": {"nos": {"last_success_at": "2026-11-23T17:00:00+00:00"}}
                }))
                payload = ps.write_status(status_path, rows=10, legacy=res)
                self.assertEqual(
                    payload["dataset_details"]["nos"]["last_success_at"],
                    "2026-11-23T17:00:00+00:00",
                )
                self.assertEqual(
                    payload["dataset_details"]["nos"]["last_attempt_at"],
                    res["nos_last_attempt_at"],
                )
            finally:
                os.chdir(old_cwd)

    def test_update_nos_history_partial_failure_merges_success_and_preserves_cached_actuals(self):
        today = datetime.date(2026, 11, 24)
        yesterday = today - datetime.timedelta(days=1)
        old_stamp = "2026-11-23T10:00+01:00"

        cached_rows = []
        for i in range(u.HISTORY_DAYS):
            d = today - datetime.timedelta(days=i)
            cached_rows.append({
                "Date": d.isoformat(),
                "Hour": 1,
                "Planned_Production_MW": 1000.0,
                "Actual_Production_MW": 910.0 if d == yesterday else 900.0,
                "Planned_Consumption_MW": 800.0,
                "Actual_Consumption_MW": 790.0,
                "Fetched_At": old_stamp,
            })

        def fake_fetch(session, day):
            if day == today:
                raise u.NosTimeoutError("timeout on today")
            # For yesterday, return updated Hour 1 with blank Actual_Consumption_MW
            # to verify merge_nos never erases a stored non-missing value.
            return pd.DataFrame([{
                "Date": yesterday,
                "Hour": 1,
                "Planned_Production_MW": 1050.0,
                "Actual_Production_MW": 925.0,
                "Planned_Consumption_MW": 810.0,
                "Actual_Consumption_MW": float("nan"),
            }])

        with tempfile.TemporaryDirectory() as tmp:
            old_cwd = os.getcwd()
            os.chdir(tmp)
            try:
                pd.DataFrame(cached_rows).to_csv(u.NOS_HOURLY_CSV, index=False)
                with patch.object(u, "_sarajevo_today", return_value=today), \
                     patch.object(u, "fetch_nos_day", side_effect=fake_fetch), \
                     patch("time.sleep"):
                    res = u.update_nos_history()

                self.assertEqual(res["nos_days_requested"], 2)
                self.assertEqual(res["nos_days_succeeded"], 1)
                self.assertEqual(len(res["nos_days_failed"]), 1)
                self.assertEqual(ps.DATASETS["nos"]["state"], "PARTIAL")

                saved = u.load_nos_history()
                y_row = saved[(saved["Date"] == yesterday) & (saved["Hour"] == 1)].iloc[0]
                self.assertAlmostEqual(y_row["Actual_Production_MW"], 925.0)
                # Blank in later fetch must not erase the previously stored 790.0
                self.assertAlmostEqual(y_row["Actual_Consumption_MW"], 790.0)

                t_row = saved[(saved["Date"] == today) & (saved["Hour"] == 1)].iloc[0]
                self.assertAlmostEqual(t_row["Actual_Production_MW"], 900.0)
                self.assertEqual(t_row["Fetched_At"], old_stamp)
            finally:
                os.chdir(old_cwd)

    def test_update_flows_history_success_and_connection_error_preserves_cache(self):
        now_sarajevo = datetime.datetime(2026, 11, 24, 12, 45, tzinfo=u.NOS_TZ)
        html_ok = _build_flows_html(ts_str="24.11.2026. 12:30", net_summary="-50")

        with tempfile.TemporaryDirectory() as tmp:
            old_cwd = os.getcwd()
            os.chdir(tmp)
            try:
                # 1. Mocked success writes flows_snapshots.csv and marks state OK
                resp_ok = MagicMock()
                resp_ok.status_code = 200
                resp_ok.text = html_ok
                with patch.object(u, "_nos_request", return_value=resp_ok), \
                     patch.object(u, "_sarajevo_today", return_value=now_sarajevo.date()):
                    res_ok = u.update_flows_history()

                self.assertTrue(res_ok["flows_checks_ok"])
                self.assertEqual(res_ok["flows_timestamp"], "2026-11-24 12:30")
                self.assertEqual(res_ok["flows_rows"], 1)
                cached_bytes = Path(u.FLOWS_CSV).read_bytes()

                # 2. Subsequent ConnectionError preserves cached CSV and records CONNECTION_ERROR
                with patch.dict(os.environ, {"NOS_DISABLE_CFFI_FALLBACK": "1"}), \
                     patch.object(
                         u,
                         "safe_get",
                         side_effect=std_requests.exceptions.ConnectionError("Connection aborted"),
                     ), \
                     patch("time.sleep"):
                    with self.assertRaises(u.NosConnectionError) as ctx:
                        u.update_flows_history()
                    self.assertEqual(ctx.exception.kind, "CONNECTION_ERROR")

                self.assertEqual(Path(u.FLOWS_CSV).read_bytes(), cached_bytes)
                flows_meta = ps.DATASETS["flows"]
                self.assertEqual(flows_meta["state"], "FAILED")
                self.assertEqual(flows_meta["error_kind"], "CONNECTION_ERROR")
                self.assertEqual(flows_meta["flows_timestamp"], "2026-11-24 12:30")
                self.assertEqual(flows_meta["flows_rows"], 1)
                self.assertIsNotNone(flows_meta["last_successful_fetch_at"])
                self.assertIsNotNone(flows_meta["last_attempt_at"])
            finally:
                os.chdir(old_cwd)


if __name__ == "__main__":
    unittest.main()
