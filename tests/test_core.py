import unittest

import pandas as pd

from record_reader.config import AppConfig
from record_reader.identity import canonical_ckt
from record_reader.intervals import (
    intersect_intervals,
    interval_minutes,
    merge_intervals,
)
from record_reader.loaders import detect_isp_name, parse_duration_to_minutes
from record_reader.matching import match_by_overlap
from record_reader.reports import _infer_month, build_daily_sla, get_both_isp_down


class IntervalTests(unittest.TestCase):
    def test_merge_and_intersection_do_not_double_count(self):
        base = pd.Timestamp("2026-09-01 00:00")
        left = [
            (base, base + pd.Timedelta(minutes=30)),
            (base + pd.Timedelta(minutes=20), base + pd.Timedelta(minutes=50)),
        ]
        right = [(base + pd.Timedelta(minutes=10), base + pd.Timedelta(minutes=40))]

        self.assertEqual(interval_minutes(merge_intervals(left)), 50.0)
        self.assertEqual(interval_minutes(intersect_intervals(left, right)), 30.0)

    def test_both_isp_down_merges_overlapping_events(self):
        start = pd.Timestamp("2026-09-01 00:00")
        alerts = pd.DataFrame(
            {
                "Site": ["BR-A", "BR-A", "BR-A"],
                "_ISP_Norm": ["Airtel", "Airtel", "Jio"],
                "Time": [
                    start,
                    start + pd.Timedelta(minutes=10),
                    start + pd.Timedelta(minutes=15),
                ],
                "Recovery time": [
                    start + pd.Timedelta(minutes=30),
                    start + pd.Timedelta(minutes=40),
                    start + pd.Timedelta(minutes=35),
                ],
            },
        )

        self.assertEqual(
            get_both_isp_down(
                alerts,
                "BR-A",
                "Airtel",
                "Jio",
                start,
                start + pd.Timedelta(hours=1),
            ),
            20.0,
        )


class MatchingTests(unittest.TestCase):
    def test_normalized_vendor_isp_is_unmatched_not_noc_only(self):
        cfg = AppConfig(
            isp_file_map={"JIO export": "JIO"},
            isp_normalize={"JIO": "Jio"},
        )
        start = pd.Timestamp("2026-09-01 10:00")
        noc = pd.DataFrame(
            {
                "CKT_ID": ["101"],
                "Time": [start],
                "Recovery time": [start + pd.Timedelta(minutes=10)],
                "ISP": ["JIO"],
            },
        )

        result = match_by_overlap(noc, pd.DataFrame(), cfg)

        self.assertEqual(result.loc[0, "Match_Status"], "Unmatched")

    def test_prefers_largest_overlap(self):
        cfg = AppConfig(isp_file_map={"Airtel export": "Airtel"})
        start = pd.Timestamp("2026-09-01 10:00")
        noc = pd.DataFrame(
            {
                "CKT_ID": ["101"],
                "Time": [start],
                "Recovery time": [start + pd.Timedelta(minutes=30)],
                "ISP": ["Airtel"],
            },
        )
        vendor = pd.DataFrame(
            {
                "CKT_ID": ["101", "101"],
                "ISP_Start": [
                    start - pd.Timedelta(minutes=5),
                    start + pd.Timedelta(minutes=5),
                ],
                "ISP_End": [
                    start + pd.Timedelta(minutes=15),
                    start + pd.Timedelta(minutes=25),
                ],
                "ISP_Name": ["Airtel", "Airtel"],
            },
        )

        result = match_by_overlap(noc, vendor, cfg)

        self.assertEqual(result.loc[0, "Match_Status"], "Matched")
        self.assertEqual(result.loc[0, "Overlap_min"], 20.0)


class LoaderAndIdentityTests(unittest.TestCase):
    def test_parse_duration_handles_days_weeks_and_hours(self):
        self.assertEqual(parse_duration_to_minutes("1d 12h 17m"), 2177.0)
        self.assertEqual(parse_duration_to_minutes("2d 5h 9m"), 3189.0)
        self.assertEqual(parse_duration_to_minutes("1w 2d"), 12960.0)
        self.assertEqual(parse_duration_to_minutes("45m 30s"), 45.5)
        self.assertEqual(parse_duration_to_minutes(None), 0.0)

    def test_canonical_ckt_preserves_internal_dot_zero(self):
        self.assertEqual(canonical_ckt("10.0.1.2"), "10.0.1.2")
        self.assertEqual(canonical_ckt("CKT-10.0.5"), "CKT-10.0.5")
        self.assertEqual(canonical_ckt("12345.0"), "12345")
        self.assertEqual(canonical_ckt("1.5e+04"), "15000")
        self.assertEqual(canonical_ckt(None), "")
        self.assertEqual(canonical_ckt("nan"), "")

    def test_detect_isp_name_prefers_longer_matching_prefix(self):
        cfg = AppConfig(
            isp_file_map={
                "Haier Uptime": "Airtel",
                "Haier Uptime report": "JIO",
            },
        )
        self.assertEqual(
            detect_isp_name("Haier Uptime report Sep.xlsx", cfg),
            "JIO",
        )
        self.assertEqual(
            detect_isp_name("Haier Uptime Sep.xlsx", cfg),
            "Airtel",
        )

    def test_infer_month_handles_empty_and_all_nat(self):
        df_empty = pd.DataFrame()
        df_nat = pd.DataFrame({"Time": [pd.NaT, pd.NaT]})
        self.assertIsNone(_infer_month(df_empty, df_nat))

        df_valid = pd.DataFrame({"Time": [pd.Timestamp("2026-09-15")]})
        self.assertEqual(
            _infer_month(df_valid, df_empty),
            pd.Period("2026-09", freq="M"),
        )

    def test_build_daily_sla_handles_empty_sites_cleanly(self):
        df = pd.DataFrame(
            {
                "Time": [pd.Timestamp("2026-09-01")],
                "Recovery time": [pd.Timestamp("2026-09-01 01:00")],
            },
        )
        result = build_daily_sla(
            df,
            pd.DataFrame(),
            [],
            {},
            pd.Period("2026-09", freq="M"),
        )
        self.assertTrue(result.empty)
        self.assertIn("Daily SLA %", result.columns)


if __name__ == "__main__":
    unittest.main()
