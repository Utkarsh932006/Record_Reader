import unittest

import pandas as pd

from record_reader.config import AppConfig
from record_reader.identity import canonical_ckt
from record_reader.intervals import (
    event_touches_working_hours,
    intersect_intervals,
    interval_minutes,
    is_overnight_shutdown,
    merge_intervals,
)
from record_reader.loaders import detect_isp_name, parse_duration_to_minutes
from record_reader.matching import match_by_overlap
from record_reader.reports import (
    _infer_month,
    build_daily_sla,
    build_summary,
    get_both_isp_down,
)


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


class WorkingHoursTests(unittest.TestCase):
    def test_is_overnight_shutdown_detects_office_power_off(self):
        wed_evening = pd.Timestamp("2026-09-02 18:30")
        thu_morning = pd.Timestamp("2026-09-03 09:15")
        self.assertTrue(is_overnight_shutdown(wed_evening, thu_morning))

        fri_evening = pd.Timestamp("2026-09-04 19:00")
        mon_morning = pd.Timestamp("2026-09-07 09:00")
        self.assertTrue(is_overnight_shutdown(fri_evening, mon_morning))

    def test_is_overnight_shutdown_ignores_genuine_outages(self):
        midday_start = pd.Timestamp("2026-09-02 14:00")
        thu_morning = pd.Timestamp("2026-09-03 11:00")
        self.assertFalse(is_overnight_shutdown(midday_start, thu_morning))

        same_day_start = pd.Timestamp("2026-09-02 10:00")
        same_day_end = pd.Timestamp("2026-09-02 12:00")
        self.assertFalse(is_overnight_shutdown(same_day_start, same_day_end))

        self.assertFalse(is_overnight_shutdown(pd.NaT, thu_morning))

    def test_event_touches_working_hours(self):
        wh_start = pd.Timestamp("2026-09-02 14:00")  # Wed 2pm
        wh_end = pd.Timestamp("2026-09-02 15:00")
        self.assertTrue(event_touches_working_hours(wh_start, wh_end))

        night_start = pd.Timestamp("2026-09-02 02:00")  # Wed 2am
        night_end = pd.Timestamp("2026-09-02 04:00")
        self.assertFalse(event_touches_working_hours(night_start, night_end))

        sun_start = pd.Timestamp("2026-09-06 12:00")  # Sun
        sun_end = pd.Timestamp("2026-09-06 15:00")
        self.assertFalse(event_touches_working_hours(sun_start, sun_end))

    def test_working_hours_sla_calculation(self):
        cfg = AppConfig(
            working_hours_enabled=True,
            working_hours_start=9,
            working_hours_end=19,
            working_days=(0, 1, 2, 3, 4),
            ignore_overnight_shutdowns=True,
        )
        # Tuesday 2026-09-01 outage from 10:00 to 11:00 (60 minutes down)
        fw = pd.DataFrame(
            {
                "Site": ["BR-A"],
                "Time": [pd.Timestamp("2026-09-01 10:00")],
                "Recovery time": [pd.Timestamp("2026-09-01 11:00")],
            },
        )
        daily = build_daily_sla(
            pd.DataFrame(),
            fw,
            ["BR-A"],
            {"BR-A": {}},
            pd.Period("2026-09", freq="M"),
            cfg=cfg,
        )
        tue_row = daily[daily["Date"] == "01-09-2026"].iloc[0]
        self.assertEqual(tue_row["Actual Site Down (min)"], 60.0)
        # 600 - 60 / 600 = 0.90
        self.assertAlmostEqual(tue_row["Daily SLA %"], 0.90, places=4)

        # Saturday 2026-09-05 is a weekend so it must not be in Daily SLA
        self.assertTrue(daily[daily["Date"] == "05-09-2026"].empty)
        self.assertEqual(daily["Date"].nunique(), 22)

        # Monthly summary: 22 working days * 600 = 13200 total working minutes
        # SLA % = (13200 - 60) / 13200 = ~99.545%
        summary = build_summary(daily, fw, pd.DataFrame(), cfg=cfg)
        self.assertEqual(summary.loc[0, "Actual Down Total (min)"], 60.0)
        self.assertAlmostEqual(
            summary.loc[0, "Monthly SLA %"], 13140.0 / 13200.0, places=4
        )
        # Average of Daily SLA % across working days matches Monthly SLA % exactly
        self.assertAlmostEqual(
            daily["Daily SLA %"].mean(),
            summary.loc[0, "Monthly SLA %"],
            places=6,
        )


if __name__ == "__main__":
    unittest.main()
