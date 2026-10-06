import unittest

import pandas as pd

from record_reader.config import AppConfig
from record_reader.intervals import (
    intersect_intervals,
    interval_minutes,
    merge_intervals,
)
from record_reader.matching import match_by_overlap
from record_reader.reports import get_both_isp_down


class IntervalTests(unittest.TestCase):
    def test_merge_and_intersection_do_not_double_count(self):
        base = pd.Timestamp("2026-09-01 00:00")
        left = [(base, base + pd.Timedelta(minutes=30)), (base + pd.Timedelta(minutes=20), base + pd.Timedelta(minutes=50))]
        right = [(base + pd.Timedelta(minutes=10), base + pd.Timedelta(minutes=40))]

        self.assertEqual(interval_minutes(merge_intervals(left)), 50.0)
        self.assertEqual(interval_minutes(intersect_intervals(left, right)), 30.0)

    def test_both_isp_down_merges_overlapping_events(self):
        start = pd.Timestamp("2026-09-01 00:00")
        alerts = pd.DataFrame(
            {
                "Site": ["BR-A", "BR-A", "BR-A"],
                "_ISP_Norm": ["Airtel", "Airtel", "Jio"],
                "Time": [start, start + pd.Timedelta(minutes=10), start + pd.Timedelta(minutes=15)],
                "Recovery time": [start + pd.Timedelta(minutes=30), start + pd.Timedelta(minutes=40), start + pd.Timedelta(minutes=35)],
            }
        )

        self.assertEqual(
            get_both_isp_down(
                alerts, "BR-A", "Airtel", "Jio", start, start + pd.Timedelta(hours=1)
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
            }
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
            }
        )
        vendor = pd.DataFrame(
            {
                "CKT_ID": ["101", "101"],
                "ISP_Start": [start - pd.Timedelta(minutes=5), start + pd.Timedelta(minutes=5)],
                "ISP_End": [start + pd.Timedelta(minutes=15), start + pd.Timedelta(minutes=25)],
                "ISP_Name": ["Airtel", "Airtel"],
            }
        )

        result = match_by_overlap(noc, vendor, cfg)

        self.assertEqual(result.loc[0, "Match_Status"], "Matched")
        self.assertEqual(result.loc[0, "Overlap_min"], 20.0)


if __name__ == "__main__":
    unittest.main()
