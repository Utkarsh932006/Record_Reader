from __future__ import annotations

import pandas as pd

from record_reader.config import AppConfig
from record_reader.identity import canonical_ckt, normalize_isp
from record_reader.intervals import overlap_minutes


def match_by_overlap(
    noc_df: pd.DataFrame,
    isp_df: pd.DataFrame,
    cfg: AppConfig,
    ckt_sites: dict[str, list[str]] | None = None,
) -> pd.DataFrame:
    """Match NOC alerts to vendor tickets by CKT_ID and overlapping time.

    Picks the vendor row with the longest overlap (ties: earliest start).
    Unmatched vendor tickets are emitted as ISP Only.
    """
    slack = pd.Timedelta(minutes=cfg.overlap_slack_minutes)
    known = cfg.known_vendor_isps
    used_isp: set[int] = set()
    rows: list[dict] = []
    ckt_sites = ckt_sites or {}

    isp_groups: dict[str, pd.DataFrame] = {}
    if not isp_df.empty:
        isp_work = isp_df.copy()
        isp_work["CKT_ID"] = isp_work["CKT_ID"].map(canonical_ckt)
        isp_groups = {ckt: g for ckt, g in isp_work.groupby("CKT_ID", sort=False)}

    if not noc_df.empty:
        for _, noc_row in noc_df.iterrows():
            ckt = canonical_ckt(noc_row.get("CKT_ID"))
            noc_start, noc_end = noc_row["Time"], noc_row["Recovery time"]
            isp_raw = noc_row["ISP"]
            base = noc_row.to_dict()
            base["CKT_ID"] = ckt

            candidates = isp_groups.get(ckt)
            best_idx = None
            best_score = -1.0
            best_start = pd.NaT
            if candidates is not None:
                for idx, isp_row in candidates.iterrows():
                    score = overlap_minutes(
                        noc_start,
                        noc_end,
                        isp_row["ISP_Start"],
                        isp_row["ISP_End"],
                        slack,
                    )
                    if score < 0:
                        continue
                    if score > best_score or (
                        score == best_score
                        and (
                            pd.isna(best_start)
                            or isp_row["ISP_Start"] < best_start
                        )
                    ):
                        best_score = score
                        best_idx = idx
                        best_start = isp_row["ISP_Start"]

            if best_idx is not None:
                used_isp.add(best_idx)
                best = isp_df.loc[best_idx]
                rows.append(
                    {
                        **base,
                        "ISP_Start": best["ISP_Start"],
                        "ISP_End": best["ISP_End"],
                        "Match_Status": "Matched",
                        "Overlap_min": round(best_score, 2),
                    }
                )
            elif normalize_isp(isp_raw, cfg) not in known:
                rows.append(
                    {
                        **base,
                        "ISP_Start": pd.NaT,
                        "ISP_End": pd.NaT,
                        "Match_Status": "NOC Only",
                        "Overlap_min": 0.0,
                    }
                )
            else:
                rows.append(
                    {
                        **base,
                        "ISP_Start": pd.NaT,
                        "ISP_End": pd.NaT,
                        "Match_Status": "Unmatched",
                        "Overlap_min": 0.0,
                    }
                )

    if not isp_df.empty:
        for idx, isp_row in isp_df.iterrows():
            if idx in used_isp:
                continue
            ckt = canonical_ckt(isp_row["CKT_ID"])
            sites = ckt_sites.get(ckt, [])
            site = sites[0] if len(sites) == 1 else (sites[0] if sites else "")
            if len(sites) > 1:
                site = ""
            rows.append(
                {
                    "Site": site,
                    "CKT_ID": ckt,
                    "ISP": isp_row.get("ISP_Name", ""),
                    "Host": "",
                    "Time": pd.NaT,
                    "Recovery time": pd.NaT,
                    "Duration_min": 0.0,
                    "ISP_Start": isp_row["ISP_Start"],
                    "ISP_End": isp_row["ISP_End"],
                    "Match_Status": "ISP Only",
                    "Overlap_min": 0.0,
                    "_ISP_Norm": normalize_isp(isp_row.get("ISP_Name"), cfg),
                }
            )

    return pd.DataFrame(rows)
