import re
import warnings
from glob import glob
from pathlib import Path

import numpy as np
import pandas as pd
from fuzzywuzzy import process

warnings.filterwarnings("ignore", category=UserWarning, module="openpyxl")

# === USER CONFIG ===
DOCS_DIR = Path(__file__).resolve().parent.parent.parent / "Docs"
OUTPUT_FILE = DOCS_DIR / "Generated_ISP_Report.xlsx"
MASTER_MAPPING_FILE = "Site_ISP_Master_Mapping.xlsx"
TABLE_STYLE = (
    "TableStyleLight9"  # e.g. TableStyleMedium2, TableStyleMedium9, TableStyleLight9
)

# Auto-detect file patterns (glob). First match wins.
NOC_PATTERN = "Haier ISP *.xlsx"
FIREWALL_PATTERN = "Haier Firewall *.xlsx"

# Map filename substring → ISP name. Order matters: first match wins.
# Add new ISPs here when their hardware log files arrive.
ISP_FILE_MAP = {
    "Haier Uptime report": "JIO",
    "Haier Uptime": "Airtel",
    # "SomeFilename": "Ishan",
}

# Column mapping per ISP. Set sheet to None to auto-detect.
ISP_COLUMN_MAP = {
    "Airtel": {
        "circuit_id": "SI Number",
        "start": "SR Creation",
        "end": "Resolved Time",
        "sheet": None,
    },
    "JIO": {
        "circuit_id": "Service Id",
        "start": "Creation Date & Time",
        "end": "Resolution Date & Time",
        "sheet": "Tickets",
    },
    # Add new ISPs here with their column names
}

# Normalize NOC ISP names to canonical column names
ISP_NORMALIZE = {
    "Airtel": "Airtel",
    "Airtel_CKT": "Airtel",
    "JIO": "Jio",
    "JIO_CKT": "Jio",
    "Ishan": "Ishan",
    "CountryLink_103.134.44.66": "CountryLink",
    "CDC": "Airtel",
    # Add new ISP aliases here
}
ISP_COLUMNS = ["Airtel", "Jio", "Ishan", "CountryLink"]

# City name aliases for firewall hostname resolution
CITY_ALIASES = {
    "GURGAON": "GURUGRAM",
    "PUNECDC": "PUNE-CDC",
}


# ── Utilities ──────────────────────────────────────────────────────────


def parse_duration_to_minutes(duration_str):
    """Parse duration string like '1h 26m', '59m', '2m 1s' into float minutes."""
    if pd.isna(duration_str):
        return 0.0
    s = str(duration_str)
    hours = re.search(r"(\d+)h", s)
    minutes = re.search(r"(\d+)m", s)
    seconds = re.search(r"(\d+)s", s)
    total = 0.0
    if hours:
        total += int(hours.group(1)) * 60
    if minutes:
        total += int(minutes.group(1))
    if seconds:
        total += int(seconds.group(1)) / 60.0
    return round(total, 2)


def find_file(docs_dir, pattern, exclude=None):
    """Find the first file matching a glob pattern, excluding specific names."""
    exclude = set(exclude or [])
    for path in sorted(glob(str(docs_dir / pattern))):
        if Path(path).name not in exclude:
            return Path(path)
    return None


def merge_intervals(intervals):
    """Merge overlapping (start, end) intervals and return total minutes."""
    if not intervals:
        return 0.0
    intervals.sort(key=lambda x: x[0])
    merged = [intervals[0]]
    for s, e in intervals[1:]:
        prev_s, prev_e = merged[-1]
        if s <= prev_e:
            merged[-1] = (prev_s, max(prev_e, e))
        else:
            merged.append((s, e))
    return sum((e - s).total_seconds() / 60.0 for s, e in merged)


def get_interval_down(df, site, start_dt, end_dt, isp_name=None):
    """Calculate total down minutes for a site in a time window, merging overlaps."""
    if df.empty:
        return 0.0
    mask = (
        (df["Site"] == site)
        & (df["Time"] < end_dt)
        & (df["Recovery time"] > start_dt)
    )
    if isp_name is not None:
        mask &= df["_ISP_Norm"] == isp_name
    events = df[mask]
    if events.empty:
        return 0.0

    intervals = []
    for _, r in events.iterrows():
        s = max(r["Time"], start_dt)
        e = min(r["Recovery time"], end_dt)
        if e > s:
            intervals.append((s, e))
    return merge_intervals(intervals)


def _clean_ckt(val):
    """Clean a CKT ID value, returning empty string for invalid values."""
    s = str(val or "")
    if s in ("Unknown_Airtel", "Unknown_Jio", "No_CKT", "nan", "None", ""):
        return ""
    return s.replace(".0", "")


# ── Fuzzy Site Matching ────────────────────────────────────────────────


def fuzzy_match_site(host, master_sites):
    """Resolve a firewall hostname to a master site name using fuzzy matching."""
    if not master_sites:
        return None
    host_str = str(host)

    # Apply known city aliases
    for alias, canonical in CITY_ALIASES.items():
        host_str = re.sub(alias, canonical, host_str, flags=re.IGNORECASE)

    # Try structured format: City-Branch/Warehouse-FW-IP (FW is optional)
    match = re.search(
        r"([A-Za-z][A-Za-z-]*?)-(Branch|Warehouse|Zonal Branch)-(?:FW-?)?",
        host_str,
        re.IGNORECASE,
    )
    if match:
        city = match.group(1).upper()
        typ = match.group(2).upper()
        prefix = "WH" if "WAREHOUSE" in typ else "BR"
        site = f"{prefix}-{city}"

        # Exact match
        for ms in master_sites:
            if ms.upper() == site:
                return ms

        # Fuzzy match the city part
        city_parts = [ms.split("-", 1)[-1] for ms in master_sites]
        best, score = process.extractOne(city, city_parts)
        if score > 80:
            # Try with correct prefix first
            for ms in master_sites:
                if ms.upper() == f"{prefix}-{best}".upper():
                    return ms
            # Fallback: any prefix with that city
            for ms in master_sites:
                if best.upper() in ms.upper():
                    return ms

    # Fallback: extract first segment as city, check for type hints
    parts = host_str.split("-")
    if len(parts) > 1:
        city = parts[0].upper()
        for alias, canonical in CITY_ALIASES.items():
            if city == alias:
                city = canonical
        has_warehouse = any("WAREHOUSE" in p.upper() for p in parts)
        prefix = "WH" if has_warehouse else "BR"

        city_parts = [ms.split("-", 1)[-1] for ms in master_sites]
        if city_parts:
            best, score = process.extractOne(city, city_parts)
            if score > 80:
                # Try with detected prefix first
                for ms in master_sites:
                    if ms.upper() == f"{prefix}-{best}".upper():
                        return ms
                for ms in master_sites:
                    if best.upper() in ms.upper():
                        return ms
    return None


# ── NOC Host Parsing ──────────────────────────────────────────────────


def parse_host(host):
    """Extract (site, isp, ckt_id) from a NOC ISP hostname."""
    h = str(host)
    site = "-".join(h.split("-")[:2])
    parts = h.split("-")
    isp = parts[2] if len(parts) > 2 else "Unknown"

    m = re.search(r"CK[TD](?:-ID)?-(\S+?)_", h, re.IGNORECASE)
    if not m:
        m = re.search(r"CK[TD](?:-ID)?-(\S+)", h, re.IGNORECASE)
    ckt = m.group(1) if m else None

    # Edge-case overrides for non-standard hostnames
    if "GURUGRAM WH JIO" in h:
        site = "WH-Gurugram"
        isp = "JIO"
        m2 = re.search(r"CKTID\s*-\s*(\S+?)-", h)
        ckt = m2.group(1) if m2 else ckt

    if "Nagpur-Ishan146783" in h:
        site, isp, ckt = "WH-NAGPUR", "Ishan", "146783"

    if "WH-Ishan-HUBLI" in h:
        site, isp = "WH-HUBLI", "Ishan"

    if "BR-JIO-Vijayawada" in h:
        site, isp = "BR-Vijayawada", "JIO"

    if "BR-PUNE-CDC" in h or "WH-PUNE-CDC" in h:
        if h.startswith("WH-PUNE-CDC"):
            site = "WH-PUNE-CDC"
            isp = parts[3] if len(parts) > 3 else "Unknown"

    return site, isp, ckt


def extract_ckt_id(host):
    return parse_host(host)[2]


def extract_site(host):
    return parse_host(host)[0]


def extract_isp(host):
    return parse_host(host)[1]


# ── Data Loading ──────────────────────────────────────────────────────


def load_noc(path):
    """Load and parse the NOC ISP alerts file."""
    df = pd.read_excel(path, sheet_name="Row data ")
    df["CKT_ID"] = df["Host"].apply(extract_ckt_id)
    df["Site"] = df["Host"].apply(extract_site)
    df["ISP"] = df["Host"].apply(extract_isp)
    df["Time"] = pd.to_datetime(df["Time"])
    df["Recovery time"] = pd.to_datetime(df["Recovery time"])
    df["Duration_min"] = df["Duration"].apply(parse_duration_to_minutes)
    # Pre-compute normalized ISP for faster filtering
    df["_ISP_Norm"] = df["ISP"].map(lambda x: ISP_NORMALIZE.get(x, x))
    return df


def load_firewall(path, master_sites):
    """Load and parse the Firewall alerts file, mapping hosts to master sites."""
    df = pd.read_excel(path, sheet_name="Row data ")
    df["Site"] = df["Host"].apply(lambda x: fuzzy_match_site(x, master_sites))
    df["Time"] = pd.to_datetime(df["Time"])
    df["Recovery time"] = pd.to_datetime(df["Recovery time"])
    df["Duration_min"] = df["Duration"].apply(parse_duration_to_minutes)
    return df


def detect_isp_name(filename):
    """Detect ISP name from filename using ISP_FILE_MAP."""
    for pattern, isp in ISP_FILE_MAP.items():
        if pattern in filename:
            return isp
    return None


def load_isp_file(path, isp_name):
    """Load a single ISP hardware log file."""
    col_map = ISP_COLUMN_MAP.get(isp_name)
    if not col_map:
        return pd.DataFrame()

    sheet = col_map["sheet"]
    if sheet:
        df = pd.read_excel(path, sheet_name=sheet)
    else:
        for s in pd.ExcelFile(path).sheet_names:
            df = pd.read_excel(path, sheet_name=s)
            if col_map["circuit_id"] in df.columns and col_map["start"] in df.columns:
                break
        else:
            return pd.DataFrame()

    cid, start, end = col_map["circuit_id"], col_map["start"], col_map["end"]
    if not all(c in df.columns for c in [cid, start, end]):
        return pd.DataFrame()

    df = df.dropna(subset=[cid, start, end])
    df = df.rename(columns={cid: "CKT_ID", start: "ISP_Start", end: "ISP_End"})
    df["CKT_ID"] = df["CKT_ID"].astype(str).str.replace(r"\.0$", "", regex=True)
    df["ISP_Start"] = pd.to_datetime(df["ISP_Start"])
    df["ISP_End"] = pd.to_datetime(df["ISP_End"])
    df["ISP_Name"] = isp_name
    return df[["CKT_ID", "ISP_Start", "ISP_End", "ISP_Name"]]


def load_all_isp_files(docs_dir, exclude_names=None):
    """Discover and load all ISP hardware log files from the docs directory."""
    exclude = set(exclude_names or [])
    frames = []
    for path in sorted(glob(str(docs_dir / "*.xlsx"))):
        fname = Path(path).name
        if fname in exclude:
            continue
        isp_name = detect_isp_name(fname)
        if not isp_name:
            continue
        df = load_isp_file(path, isp_name)
        if not df.empty:
            frames.append(df)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


# ── Matching ──────────────────────────────────────────────────────────


def match_by_overlap(noc_df, isp_df):
    """Match NOC alerts to ISP records by overlapping time intervals on the same CKT_ID."""
    known_isps = set(ISP_FILE_MAP.values())
    results = []

    for _, noc_row in noc_df.iterrows():
        ckt = noc_row["CKT_ID"]
        noc_start, noc_end = noc_row["Time"], noc_row["Recovery time"]
        isp_name = noc_row["ISP"]

        if isp_df.empty or ckt is None:
            status = "NOC Only" if isp_name not in known_isps else "Unmatched"
            results.append(
                {
                    **noc_row,
                    "ISP_Start": pd.NaT,
                    "ISP_End": pd.NaT,
                    "Match_Status": status,
                }
            )
            continue

        # Find ISP records for same CKT with overlapping time window
        ckt_isps = isp_df[isp_df["CKT_ID"] == ckt]
        overlaps = ckt_isps[
            (ckt_isps["ISP_Start"] < noc_end) & (ckt_isps["ISP_End"] > noc_start)
        ]

        if not overlaps.empty:
            best = overlaps.iloc[0]
            results.append(
                {
                    **noc_row,
                    "ISP_Start": best["ISP_Start"],
                    "ISP_End": best["ISP_End"],
                    "Match_Status": "Matched",
                }
            )
        elif isp_name not in known_isps:
            results.append(
                {
                    **noc_row,
                    "ISP_Start": pd.NaT,
                    "ISP_End": pd.NaT,
                    "Match_Status": "NOC Only",
                }
            )
        else:
            results.append(
                {
                    **noc_row,
                    "ISP_Start": pd.NaT,
                    "ISP_End": pd.NaT,
                    "Match_Status": "Unmatched",
                }
            )

    return pd.DataFrame(results)


# ── Report Builders ───────────────────────────────────────────────────


def _get_report_month(noc_df, fw_df):
    """Determine the report month from the available data."""
    if not noc_df.empty:
        return noc_df["Time"].dt.to_period("M").mode()[0]
    if not fw_df.empty:
        return fw_df["Time"].dt.to_period("M").mode()[0]
    return None


def build_daily_sla(noc_df, fw_df, master_sites, site_mapping):
    """Daily SLA sheet: one row per site per day, sorted by Site → Date."""
    report_month = _get_report_month(noc_df, fw_df)
    if report_month is None:
        return pd.DataFrame()

    all_days = pd.date_range(
        report_month.start_time, periods=report_month.day, freq="D"
    )
    rows = []
    for d in all_days:
        d_end = d + pd.Timedelta(days=1)
        for s in master_sites:
            mapping = site_mapping.get(s, {})
            isp1 = mapping.get("ISP1 Name") or ""
            isp2 = mapping.get("ISP2 Name") or ""
            ckt1 = _clean_ckt(mapping.get("ISP1 CKT ID"))
            ckt2 = _clean_ckt(mapping.get("ISP2 CKT ID"))

            down1 = get_interval_down(noc_df, s, d, d_end, isp1) if isp1 else 0.0
            down2 = get_interval_down(noc_df, s, d, d_end, isp2) if isp2 else 0.0
            actual_down = get_interval_down(fw_df, s, d, d_end)

            rows.append(
                {
                    "Date": d.strftime("%d-%m-%Y"),
                    "Site": s,
                    "ISP1 Name": isp1,
                    "ISP1 CKT ID": ckt1,
                    "ISP1 Down (min)": round(down1, 2),
                    "ISP2 Name": isp2,
                    "ISP2 CKT ID": ckt2,
                    "ISP2 Down (min)": round(down2, 2),
                    "Actual Site Down (min)": round(actual_down, 2),
                    "Daily SLA %": (1440 - actual_down) / 1440.0,
                }
            )

    df_result = pd.DataFrame(rows)
    df_result["_sort"] = pd.to_datetime(df_result["Date"], format="%d-%m-%Y")
    return (
        df_result.sort_values(["Site", "_sort"])
        .drop(columns=["_sort"])
        .reset_index(drop=True)
    )


def build_circuit_details(matched_df):
    """Circuit detail sheet: one row per NOC event, sorted by Site → CKT ID."""
    if matched_df.empty:
        return pd.DataFrame()
    fmt = lambda t: t.strftime("%d-%m-%Y %H:%M:%S") if pd.notna(t) else ""
    rows = [
        {
            "Site": r["Site"],
            "CKT ID": str(r.get("CKT_ID", "") or ""),
            "ISP Name": r["ISP"],
            "Duration (min)": r.get("Duration_min", 0),
            "Match Status": r.get("Match_Status", ""),
            "NOC Start": fmt(r["Time"]),
            "NOC End": fmt(r["Recovery time"]),
            "ISP Start": fmt(r.get("ISP_Start")),
            "ISP End": fmt(r.get("ISP_End")),
        }
        for _, r in matched_df.iterrows()
    ]
    df = pd.DataFrame(rows)
    return df.sort_values(["Site", "CKT ID"]).reset_index(drop=True)


def build_firewall_details(fw_df):
    """Firewall details sheet: one row per event with mapping status."""
    if fw_df.empty:
        return pd.DataFrame()
    fmt = lambda t: t.strftime("%d-%m-%Y %H:%M:%S") if pd.notna(t) else ""
    rows = [
        {
            "Host": r["Host"],
            "Mapped Site": r["Site"] if pd.notna(r.get("Site")) else "⚠ UNMAPPED",
            "Start": fmt(r["Time"]),
            "End": fmt(r["Recovery time"]),
            "Duration (min)": r.get("Duration_min", 0),
            "Severity": r.get("Severity", ""),
        }
        for _, r in fw_df.iterrows()
    ]
    df = pd.DataFrame(rows)
    return df.sort_values(["Mapped Site", "Start"]).reset_index(drop=True)


def build_summary(daily_sla_df, fw_df, noc_df):
    """Monthly summary: one row per site with aggregated metrics."""
    if daily_sla_df.empty:
        return pd.DataFrame()

    summary = (
        daily_sla_df.groupby("Site")
        .agg(
            ISP1_Down=("ISP1 Down (min)", "sum"),
            ISP2_Down=("ISP2 Down (min)", "sum"),
            Actual_Down=("Actual Site Down (min)", "sum"),
        )
        .reset_index()
    )

    num_days = daily_sla_df["Date"].nunique()
    total_minutes = num_days * 1440.0
    summary["Monthly SLA %"] = (
        (total_minutes - summary["Actual_Down"]) / total_minutes
    )

    # Worst day per site
    worst_idx = daily_sla_df.groupby("Site")["Actual Site Down (min)"].idxmax()
    worst = daily_sla_df.loc[worst_idx][
        ["Site", "Date", "Actual Site Down (min)"]
    ].rename(
        columns={"Date": "Worst Day", "Actual Site Down (min)": "Worst Day Down (min)"}
    )
    summary = summary.merge(worst, on="Site", how="left")

    # Firewall incident count
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

    # NOC incident count
    if not noc_df.empty:
        noc_counts = (
            noc_df.groupby("Site").size().reset_index(name="NOC Incidents")
        )
        summary = summary.merge(noc_counts, on="Site", how="left")
        summary["NOC Incidents"] = summary["NOC Incidents"].fillna(0).astype(int)
    else:
        summary["NOC Incidents"] = 0

    summary = summary.rename(
        columns={
            "ISP1_Down": "ISP1 Down Total (min)",
            "ISP2_Down": "ISP2 Down Total (min)",
            "Actual_Down": "Actual Down Total (min)",
        }
    )

    col_order = [
        "Site",
        "NOC Incidents",
        "FW Incidents",
        "ISP1 Down Total (min)",
        "ISP2 Down Total (min)",
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


def build_minute_sheets(noc_df, fw_df, master_sites, site_mapping):
    """Minute-by-minute ISP up/down status per site, split into weekly DataFrames.

    Uses vectorized numpy operations for performance.
    """
    report_month = _get_report_month(noc_df, fw_df)
    if report_month is None:
        return []

    month_start = report_month.start_time
    total_days = report_month.day
    total_minutes = total_days * 1440

    # Build per-site, per-ISP boolean down-arrays
    sorted_sites = sorted(master_sites)
    site_down = {
        site: {isp: np.zeros(total_minutes, dtype=bool) for isp in ISP_COLUMNS}
        for site in sorted_sites
    }
    for _, r in noc_df.iterrows():
        site = r["Site"]
        isp_norm = ISP_NORMALIZE.get(r["ISP"], r["ISP"])
        if isp_norm not in site_down.get(site, {}):
            continue
        s = max(0, int((r["Time"] - month_start).total_seconds() // 60))
        e = min(
            total_minutes,
            int((r["Recovery time"] - month_start).total_seconds() // 60),
        )
        if s < e:
            site_down[site][isp_norm][s:e] = True

    # Build per-site firewall down-arrays
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

    # Pre-compute site metadata
    site_meta = {}
    for site in sorted_sites:
        mapping = site_mapping.get(site, {})
        isp1 = mapping.get("ISP1 Name") or ""
        isp2 = mapping.get("ISP2 Name") or ""
        ckt1 = _clean_ckt(mapping.get("ISP1 CKT ID"))
        ckt2 = _clean_ckt(mapping.get("ISP2 CKT ID"))
        active = {i for i in [isp1, isp2] if i}
        ckt_str = ",".join(filter(None, [ckt1, ckt2]))
        site_meta[site] = {"active": active, "ckt_str": ckt_str}

    # Generate weekly DataFrames (vectorized)
    weeks = []
    day, week_num = 0, 1
    n_sites = len(sorted_sites)

    while day < total_days:
        week_days = min(7, total_days - day)
        w_start = day * 1440
        w_end = (day + week_days) * 1440
        w_minutes = w_end - w_start

        # Pre-compute time strings for this week
        time_index = pd.date_range(
            month_start + pd.Timedelta(minutes=w_start),
            periods=w_minutes,
            freq="min",
        )
        date_strs = time_index.strftime("%d-%m-%Y").values
        time_strs = time_index.strftime("%H:%M").values

        # Build columns using numpy vectorization
        locations = np.repeat(sorted_sites, w_minutes)
        ckt_ids = np.repeat(
            [site_meta[s]["ckt_str"] for s in sorted_sites], w_minutes
        )
        dates = np.tile(date_strs, n_sites)
        times = np.tile(time_strs, n_sites)

        data = {
            "Location": locations,
            "CktId": ckt_ids,
            "Date": dates,
            "Time": times,
        }

        # ISP status columns
        for col in ISP_COLUMNS:
            col_vals = []
            for site in sorted_sites:
                active = site_meta[site]["active"]
                if col in active:
                    arr = site_down[site][col][w_start:w_end]
                    col_vals.append(np.where(arr, "Down", "Up"))
                else:
                    col_vals.append(np.full(w_minutes, "", dtype=object))
            data[col] = np.concatenate(col_vals)

        # Link status from firewall
        link_vals = []
        for site in sorted_sites:
            arr = fw_down[site][w_start:w_end]
            link_vals.append(np.where(arr, "Down", "Up"))
        data["Link"] = np.concatenate(link_vals)

        weeks.append((f"Week {week_num}", pd.DataFrame(data)))
        day += week_days
        week_num += 1

    return weeks


# ── Excel Writer ──────────────────────────────────────────────────────


def write_report(sheets, output_path):
    """Write all report sheets to an Excel file with formatting.

    Args:
        sheets: list of (sheet_name, dataframe, text_cols, pct_cols) tuples.
        output_path: path to write the Excel file.
    """
    with pd.ExcelWriter(output_path, engine="xlsxwriter") as writer:
        workbook = writer.book
        pct_fmt = workbook.add_format({"num_format": "0.00%"})
        text_fmt = workbook.add_format({"num_format": "@"})

        for sheet_name, df, txt_cols, pct_cols in sheets:
            df.to_excel(writer, index=False, sheet_name=sheet_name)

        for sheet_name, df, txt_cols, pct_cols in sheets:
            if df.empty:
                continue
            worksheet = writer.sheets[sheet_name]

            # Add table
            worksheet.add_table(
                0,
                0,
                max(1, df.shape[0]),
                df.shape[1] - 1,
                {
                    "columns": [{"header": c} for c in df.columns],
                    "style": TABLE_STYLE,
                },
            )

            # Format and auto-width for each column
            for i, col_name in enumerate(df.columns):
                max_val_len = (
                    df[col_name].map(lambda x: len(str(x))).max()
                    if not df.empty
                    else 0
                )
                max_len = max(len(str(col_name)), max_val_len)

                fmt = None
                if col_name in txt_cols:
                    fmt = text_fmt
                elif col_name in pct_cols:
                    fmt = pct_fmt

                worksheet.set_column(i, i, max(max_len + 2, 12), fmt)


# ── Main ──────────────────────────────────────────────────────────────


def main() -> None:
    # Load master mapping first (needed for firewall site resolution)
    mapping_path = DOCS_DIR / MASTER_MAPPING_FILE
    if mapping_path.exists():
        site_mapping = (
            pd.read_excel(mapping_path).set_index("Location").to_dict("index")
        )
        master_sites = sorted(site_mapping.keys())
    else:
        print(f"⚠ Master mapping not found: {mapping_path}")
        site_mapping = {}
        master_sites = []
    print(f"Master site list: {len(master_sites)} sites")

    # Build exclusion set for ISP file discovery
    exclude_files = {Path(OUTPUT_FILE).name, MASTER_MAPPING_FILE}

    # Auto-detect and load NOC file
    noc_path = find_file(DOCS_DIR, NOC_PATTERN, exclude=exclude_files)
    if noc_path:
        exclude_files.add(noc_path.name)
        print(f"Loading NOC data from {noc_path.name}")
        noc_df = load_noc(noc_path)
        print(
            f"  {len(noc_df)} alerts, {noc_df['Site'].nunique()} sites, "
            f"ISPs: {sorted(noc_df['ISP'].unique())}"
        )
    else:
        print("⚠ No NOC ISP file found (pattern: {NOC_PATTERN})")
        noc_df = pd.DataFrame()

    # Auto-detect and load Firewall file
    fw_path = find_file(DOCS_DIR, FIREWALL_PATTERN, exclude=exclude_files)
    if fw_path:
        exclude_files.add(fw_path.name)
        print(f"Loading Firewall data from {fw_path.name}")
        fw_df = load_firewall(fw_path, master_sites)
        unmapped = fw_df[fw_df["Site"].isna()]
        print(
            f"  {len(fw_df)} alerts, "
            f"{len(fw_df) - len(unmapped)} mapped, "
            f"{len(unmapped)} unmapped"
        )
        if not unmapped.empty:
            print(
                f"  ⚠ Unmapped hosts: {sorted(unmapped['Host'].unique())}"
            )
    else:
        print(f"⚠ No Firewall file found (pattern: {FIREWALL_PATTERN})")
        fw_df = pd.DataFrame()

    # Load ISP hardware logs (everything remaining in Docs/)
    print("Loading ISP hardware logs...")
    isp_df = load_all_isp_files(DOCS_DIR, exclude_names=exclude_files)
    print(
        f"  {len(isp_df)} ISP records from: {sorted(isp_df['ISP_Name'].unique())}"
        if not isp_df.empty
        else "  No ISP hardware log files found"
    )

    # Match NOC alerts against ISP hardware logs
    if not noc_df.empty:
        print("Matching NOC alerts → ISP records (overlapping intervals)...")
        matched = match_by_overlap(noc_df, isp_df)
        for status in ["Matched", "NOC Only", "Unmatched"]:
            print(f"  {status}: {(matched['Match_Status'] == status).sum()}")
    else:
        matched = pd.DataFrame()

    # Build all report sheets
    print("Building reports...")
    daily_sla = build_daily_sla(noc_df, fw_df, master_sites, site_mapping)
    summary = build_summary(daily_sla, fw_df, noc_df)
    circuit_details = build_circuit_details(matched)
    fw_details = build_firewall_details(fw_df)
    weekly_sheets = build_minute_sheets(noc_df, fw_df, master_sites, site_mapping)

    # Assemble all sheets with formatting metadata
    all_sheets = [
        ("Summary", summary, [], ["Monthly SLA %"]),
        ("Daily SLA", daily_sla, ["ISP1 CKT ID", "ISP2 CKT ID"], ["Daily SLA %"]),
        ("Circuit Details", circuit_details, ["CKT ID"], []),
        ("Firewall Details", fw_details, [], []),
    ]
    for week_name, week_df in weekly_sheets:
        all_sheets.append((week_name, week_df, ["CktId"], []))

    write_report(all_sheets, OUTPUT_FILE)
    print(f"Done! → {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
