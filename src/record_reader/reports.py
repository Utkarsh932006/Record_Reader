from __future__ import annotations

import numpy as np
import pandas as pd

from record_reader.config import AppConfig
from record_reader.identity import canonical_ckt, normalize_isp
from record_reader.intervals import (
    events_to_intervals,
    intersect_intervals,
    interval_minutes,
)


def report_month_bounds(
    noc_df: pd.DataFrame,
    fw_df: pd.DataFrame,
    month: pd.Period | None = None,
) -> tuple[pd.Period | None, pd.Timestamp | None, pd.Timestamp | None]:
    if month is None:
        month = _infer_month(noc_df, fw_df)
    if month is None:
        return None, None, None
    start = month.start_time
    end = (month + 1).start_time
    return month, start, end


def _infer_month(noc_df: pd.DataFrame, fw_df: pd.DataFrame) -> pd.Period | None:
    if not noc_df.empty:
        return noc_df["Time"].dt.to_period("M").mode()[0]
    if not fw_df.empty:
        return fw_df["Time"].dt.to_period("M").mode()[0]
    return None


def _mapping_for(site: str, site_mapping: dict) -> dict:
    return site_mapping.get(site, {})


def get_interval_down(
    df: pd.DataFrame,
    site: str,
    start_dt: pd.Timestamp,
    end_dt: pd.Timestamp,
    isp_name: str | None = None,
) -> float:
    if df.empty:
        return 0.0
    mask = df["Site"] == site
    if isp_name:
        mask = mask & (df["_ISP_Norm"] == isp_name)
    subset = df.loc[mask]
    intervals = events_to_intervals(subset, "Time", "Recovery time", start_dt, end_dt)
    return interval_minutes(intervals)


def get_both_isp_down(
    noc_df: pd.DataFrame,
    site: str,
    isp1: str,
    isp2: str,
    start_dt: pd.Timestamp,
    end_dt: pd.Timestamp,
) -> float:
    if not isp1 or not isp2 or noc_df.empty:
        return 0.0
    a = noc_df[(noc_df["Site"] == site) & (noc_df["_ISP_Norm"] == isp1)]
    b = noc_df[(noc_df["Site"] == site) & (noc_df["_ISP_Norm"] == isp2)]
    left = events_to_intervals(a, "Time", "Recovery time", start_dt, end_dt)
    right = events_to_intervals(b, "Time", "Recovery time", start_dt, end_dt)
    return interval_minutes(intersect_intervals(left, right))


def build_daily_sla(
    noc_df: pd.DataFrame,
    fw_df: pd.DataFrame,
    master_sites: list[str],
    site_mapping: dict,
    month: pd.Period | None = None,
) -> pd.DataFrame:
    report_month, month_start, month_end = report_month_bounds(noc_df, fw_df, month)
    if report_month is None or month_start is None:
        return pd.DataFrame()

    days = pd.date_range(month_start, month_end - pd.Timedelta(seconds=1), freq="D")
    rows = []
    for day in days:
        day_end = day + pd.Timedelta(days=1)
        for site in master_sites:
            mapping = _mapping_for(site, site_mapping)
            isp1 = mapping.get("ISP1 Name") or ""
            isp2 = mapping.get("ISP2 Name") or ""
            if pd.isna(isp1):
                isp1 = ""
            if pd.isna(isp2):
                isp2 = ""
            ckt1 = canonical_ckt(mapping.get("ISP1 CKT ID"))
            ckt2 = canonical_ckt(mapping.get("ISP2 CKT ID"))
            down1 = get_interval_down(noc_df, site, day, day_end, isp1) if isp1 else 0.0
            down2 = get_interval_down(noc_df, site, day, day_end, isp2) if isp2 else 0.0
            both = get_both_isp_down(noc_df, site, isp1, isp2, day, day_end)
            actual = get_interval_down(fw_df, site, day, day_end)
            rows.append(
                {
                    "Date": day.strftime("%d-%m-%Y"),
                    "Site": site,
                    "ISP1 Name": isp1,
                    "ISP1 CKT ID": ckt1,
                    "ISP1 Down (min)": round(down1, 2),
                    "ISP2 Name": isp2,
                    "ISP2 CKT ID": ckt2,
                    "ISP2 Down (min)": round(down2, 2),
                    "Both ISPs Down (min)": round(both, 2),
                    "Actual Site Down (min)": round(actual, 2),
                    "Daily SLA %": (1440 - actual) / 1440.0,
                }
            )
    df_result = pd.DataFrame(rows)
    df_result["_sort"] = pd.to_datetime(df_result["Date"], format="%d-%m-%Y")
    return (
        df_result.sort_values(["Site", "_sort"])
        .drop(columns=["_sort"])
        .reset_index(drop=True)
    )


def build_circuit_details(matched_df: pd.DataFrame) -> pd.DataFrame:
    if matched_df.empty:
        return pd.DataFrame()

    def fmt(t):
        return t.strftime("%d-%m-%Y %H:%M:%S") if pd.notna(t) else ""

    rows = []
    for _, r in matched_df.iterrows():
        rows.append(
            {
                "Site": r.get("Site") or "",
                "CKT ID": canonical_ckt(r.get("CKT_ID")),
                "ISP Name": r.get("ISP") or r.get("_ISP_Norm") or "",
                "Duration (min)": r.get("Duration_min", 0),
                "Match Status": r.get("Match_Status", ""),
                "Overlap (min)": r.get("Overlap_min", 0),
                "NOC Start": fmt(r.get("Time")),
                "NOC End": fmt(r.get("Recovery time")),
                "ISP Start": fmt(r.get("ISP_Start")),
                "ISP End": fmt(r.get("ISP_End")),
            }
        )
    df = pd.DataFrame(rows)
    return df.sort_values(["Site", "CKT ID", "Match Status"]).reset_index(drop=True)


def build_firewall_details(fw_df: pd.DataFrame) -> pd.DataFrame:
    if fw_df.empty:
        return pd.DataFrame()

    def fmt(t):
        return t.strftime("%d-%m-%Y %H:%M:%S") if pd.notna(t) else ""

    rows = [
        {
            "Host": r["Host"],
            "Mapped Site": r["Site"] if pd.notna(r.get("Site")) else "UNMAPPED",
            "Start": fmt(r["Time"]),
            "End": fmt(r["Recovery time"]),
            "Duration (min)": r.get("Duration_min", 0),
            "Severity": r.get("Severity", ""),
            "Problem": r.get("Problem", ""),
        }
        for _, r in fw_df.iterrows()
    ]
    df = pd.DataFrame(rows)
    return df.sort_values(["Mapped Site", "Start"]).reset_index(drop=True)


def build_summary(daily_sla_df: pd.DataFrame, fw_df: pd.DataFrame, noc_df: pd.DataFrame) -> pd.DataFrame:
    if daily_sla_df.empty:
        return pd.DataFrame()

    summary = (
        daily_sla_df.groupby("Site")
        .agg(
            ISP1_Down=("ISP1 Down (min)", "sum"),
            ISP2_Down=("ISP2 Down (min)", "sum"),
            Both_Down=("Both ISPs Down (min)", "sum"),
            Actual_Down=("Actual Site Down (min)", "sum"),
        )
        .reset_index()
    )
    num_days = daily_sla_df["Date"].nunique()
    total_minutes = num_days * 1440.0
    summary["Monthly SLA %"] = (total_minutes - summary["Actual_Down"]) / total_minutes

    worst_idx = daily_sla_df.groupby("Site")["Actual Site Down (min)"].idxmax()
    worst = daily_sla_df.loc[worst_idx][
        ["Site", "Date", "Actual Site Down (min)"]
    ].rename(
        columns={"Date": "Worst Day", "Actual Site Down (min)": "Worst Day Down (min)"}
    )
    summary = summary.merge(worst, on="Site", how="left")

    if not fw_df.empty:
        fw_counts = (
            fw_df[fw_df["Site"].notna()]
            .groupby("Site")
            .size()
            .reset_index(name="FW Incidents")
        )
        summary = summary.merge(fw_counts, on="Site", how="left")
        summary["FW Incidents"] = summary["FW Incidents"].fillna(0).astype(int)
    else:
        summary["FW Incidents"] = 0

    if not noc_df.empty:
        noc_counts = noc_df.groupby("Site").size().reset_index(name="NOC Incidents")
        summary = summary.merge(noc_counts, on="Site", how="left")
        summary["NOC Incidents"] = summary["NOC Incidents"].fillna(0).astype(int)
        if "Duration_min" in noc_df.columns:
            mttr = (
                noc_df.groupby("Site")["Duration_min"]
                .mean()
                .reset_index(name="NOC MTTR (min)")
            )
            summary = summary.merge(mttr, on="Site", how="left")
        else:
            summary["NOC MTTR (min)"] = 0.0
    else:
        summary["NOC Incidents"] = 0
        summary["NOC MTTR (min)"] = 0.0

    summary["NOC MTTR (min)"] = summary["NOC MTTR (min)"].fillna(0).round(2)
    summary = summary.rename(
        columns={
            "ISP1_Down": "ISP1 Down Total (min)",
            "ISP2_Down": "ISP2 Down Total (min)",
            "Both_Down": "Both ISPs Down Total (min)",
            "Actual_Down": "Actual Down Total (min)",
        }
    )
    col_order = [
        "Site",
        "NOC Incidents",
        "FW Incidents",
        "NOC MTTR (min)",
        "ISP1 Down Total (min)",
        "ISP2 Down Total (min)",
        "Both ISPs Down Total (min)",
        "Actual Down Total (min)",
        "Monthly SLA %",
        "Worst Day",
        "Worst Day Down (min)",
    ]
    return (
        summary[col_order]
        .sort_values("Actual Down Total (min)", ascending=False)
        .reset_index(drop=True)
    )


def build_data_quality(
    noc_df: pd.DataFrame,
    fw_df: pd.DataFrame,
    matched: pd.DataFrame,
    master_sites: list[str],
    site_mapping: dict,
    cfg: AppConfig,
) -> pd.DataFrame:
    issues: list[dict] = []
    master_set = set(master_sites)
    master_upper = {s.upper(): s for s in master_sites}

    if not noc_df.empty:
        for host, site, ckt, isp in (
            noc_df[["Host", "Site", "CKT_ID", "ISP"]].drop_duplicates().itertuples(index=False)
        ):
            if site not in master_set:
                issues.append(
                    {
                        "Severity": "error",
                        "Category": "unmapped_noc_site",
                        "Key": host,
                        "Detail": f"Parsed site {site!r} is not in master mapping",
                    }
                )
            if not ckt:
                issues.append(
                    {
                        "Severity": "warning",
                        "Category": "missing_ckt",
                        "Key": host,
                        "Detail": f"No circuit id parsed (ISP={isp})",
                    }
                )
            elif site in site_mapping:
                mapping = site_mapping[site]
                expected = {
                    canonical_ckt(mapping.get("ISP1 CKT ID")),
                    canonical_ckt(mapping.get("ISP2 CKT ID")),
                }
                expected.discard("")
                if expected and ckt not in expected:
                    issues.append(
                        {
                            "Severity": "warning",
                            "Category": "ckt_mismatch",
                            "Key": host,
                            "Detail": f"CKT {ckt} not listed for {site} (master has {sorted(expected)})",
                        }
                    )
            norm = normalize_isp(isp, cfg)
            if norm and norm not in cfg.isp_columns:
                issues.append(
                    {
                        "Severity": "warning",
                        "Category": "unknown_isp",
                        "Key": host,
                        "Detail": f"ISP {isp!r} normalized to {norm!r} is not in isp_columns",
                    }
                )

    if not fw_df.empty:
        unmapped = fw_df[fw_df["Site"].isna() | (fw_df["Site"] == "")]
        for host in sorted(unmapped["Host"].astype(str).unique()):
            issues.append(
                {
                    "Severity": "error",
                    "Category": "unmapped_firewall_host",
                    "Key": host,
                    "Detail": "Firewall hostname did not resolve to a master site",
                }
            )

    seen: dict[str, str] = {}
    for site, row in site_mapping.items():
        for col in ("ISP1 CKT ID", "ISP2 CKT ID"):
            ckt = canonical_ckt(row.get(col))
            if not ckt:
                continue
            if ckt in seen and seen[ckt] != site:
                issues.append(
                    {
                        "Severity": "warning",
                        "Category": "duplicate_ckt",
                        "Key": ckt,
                        "Detail": f"Circuit shared by {seen[ckt]} and {site}",
                    }
                )
            else:
                seen[ckt] = site

    if not matched.empty and "Match_Status" in matched.columns:
        for status in ("Unmatched", "ISP Only", "NOC Only"):
            n = int((matched["Match_Status"] == status).sum())
            if n:
                issues.append(
                    {
                        "Severity": "info" if status == "NOC Only" else "warning",
                        "Category": "match_status",
                        "Key": status,
                        "Detail": f"{n} circuit rows with status {status}",
                    }
                )

    unused = []
    noc_sites = set(noc_df["Site"].dropna()) if not noc_df.empty else set()
    fw_sites = set(fw_df["Site"].dropna()) if not fw_df.empty else set()
    for site in master_sites:
        if site not in noc_sites and site not in fw_sites:
            unused.append(site)
    if unused:
        issues.append(
            {
                "Severity": "info",
                "Category": "sites_with_no_events",
                "Key": f"{len(unused)} sites",
                "Detail": ", ".join(unused[:20]) + ("…" if len(unused) > 20 else ""),
            }
        )

    if not issues:
        return pd.DataFrame(
            [{"Severity": "info", "Category": "ok", "Key": "", "Detail": "No data-quality issues"}]
        )
    order = {"error": 0, "warning": 1, "info": 2}
    df = pd.DataFrame(issues)
    df["_o"] = df["Severity"].map(order)
    return df.sort_values(["_o", "Category", "Key"]).drop(columns=["_o"]).reset_index(drop=True)


def build_minute_sheets(
    noc_df: pd.DataFrame,
    fw_df: pd.DataFrame,
    master_sites: list[str],
    site_mapping: dict,
    cfg: AppConfig,
    month: pd.Period | None = None,
) -> list[tuple[str, pd.DataFrame]]:
    """Optional week sheets of per-minute Up/Down. Off by default."""
    report_month, month_start, _month_end = report_month_bounds(noc_df, fw_df, month)
    if report_month is None or month_start is None:
        return []

    total_days = report_month.day
    total_minutes = total_days * 1440
    sorted_sites = sorted(master_sites)
    isp_cols = cfg.isp_columns
    site_down = {
        site: {isp: np.zeros(total_minutes, dtype=bool) for isp in isp_cols}
        for site in sorted_sites
    }
    for _, r in noc_df.iterrows():
        site = r["Site"]
        isp_norm = r.get("_ISP_Norm") or normalize_isp(r.get("ISP"), cfg)
        if isp_norm not in site_down.get(site, {}):
            continue
        s = max(0, int((r["Time"] - month_start).total_seconds() // 60))
        e = min(
            total_minutes,
            int((r["Recovery time"] - month_start).total_seconds() // 60),
        )
        if s < e:
            site_down[site][isp_norm][s:e] = True

    fw_down = {site: np.zeros(total_minutes, dtype=bool) for site in sorted_sites}
    for _, r in fw_df.iterrows():
        site = r["Site"]
        if site not in fw_down:
            continue
        s = max(0, int((r["Time"] - month_start).total_seconds() // 60))
        e = min(
            total_minutes,
            int((r["Recovery time"] - month_start).total_seconds() // 60),
        )
        if s < e:
            fw_down[site][s:e] = True

    site_meta = {}
    for site in sorted_sites:
        mapping = _mapping_for(site, site_mapping)
        isp1 = mapping.get("ISP1 Name") or ""
        isp2 = mapping.get("ISP2 Name") or ""
        if pd.isna(isp1):
            isp1 = ""
        if pd.isna(isp2):
            isp2 = ""
        ckt1 = canonical_ckt(mapping.get("ISP1 CKT ID"))
        ckt2 = canonical_ckt(mapping.get("ISP2 CKT ID"))
        site_meta[site] = {
            "active": {i for i in [isp1, isp2] if i},
            "ckt_str": ",".join(filter(None, [ckt1, ckt2])),
        }

    weeks = []
    day, week_num = 0, 1
    n_sites = len(sorted_sites)
    while day < total_days:
        week_days = min(7, total_days - day)
        w_start = day * 1440
        w_end = (day + week_days) * 1440
        w_minutes = w_end - w_start
        time_index = pd.date_range(
            month_start + pd.Timedelta(minutes=w_start),
            periods=w_minutes,
            freq="min",
        )
        date_strs = time_index.strftime("%d-%m-%Y").values
        time_strs = time_index.strftime("%H:%M").values
        locations = np.repeat(sorted_sites, w_minutes)
        ckt_ids = np.repeat([site_meta[s]["ckt_str"] for s in sorted_sites], w_minutes)
        dates = np.tile(date_strs, n_sites)
        times = np.tile(time_strs, n_sites)
        data = {
            "Location": locations,
            "CktId": ckt_ids,
            "Date": dates,
            "Time": times,
        }
        for col in isp_cols:
            col_vals = []
            for site in sorted_sites:
                active = site_meta[site]["active"]
                if col in active:
                    arr = site_down[site][col][w_start:w_end]
                    col_vals.append(np.where(arr, "Down", "Up"))
                else:
                    col_vals.append(np.full(w_minutes, "", dtype=object))
            data[col] = np.concatenate(col_vals)
        link_vals = [
            np.where(fw_down[site][w_start:w_end], "Down", "Up") for site in sorted_sites
        ]
        data["Link"] = np.concatenate(link_vals)
        weeks.append((f"Week {week_num}", pd.DataFrame(data)))
        day += week_days
        week_num += 1
    return weeks
