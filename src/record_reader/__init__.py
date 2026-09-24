import re
import warnings
from glob import glob
from pathlib import Path

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore", category=UserWarning, module="openpyxl")

# === USER CONFIG ===
DOCS_DIR = Path(__file__).resolve().parent.parent.parent / "Docs"
OUTPUT_FILE = DOCS_DIR / "Generated_ISP_Report.xlsx"
NOC_FILE = "ISP.xlsx"
MASTER_SITE_FILE = "Copy of ISP (003).xlsx"
MASTER_SITE_SHEET = "ISP"
MASTER_MAPPING_FILE = "Site_ISP_Master_Mapping.xlsx"
TABLE_STYLE = (
    "TableStyleLight9"  # e.g. TableStyleMedium2, TableStyleMedium9, TableStyleLight9
)

# Map filename substring → ISP name. Order matters: first match wins.
# Add new ISPs here when their files arrive.
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
    "Ishan": "Ishan",
    "CountryLink_103.134.44.66": "CountryLink",
    "CDC": "Airtel",
    # Add new ISP aliases here
}
ISP_COLUMNS = ["Airtel", "Jio", "Ishan", "CountryLink"]


def extract_ckt_id(host):
    m = re.search(r"CK[TD](?:-ID)?-(\d+)", str(host), re.IGNORECASE)
    return m.group(1) if m else None


def extract_site(host):
    return "-".join(str(host).split("-")[:2])


def extract_isp(host):
    parts = str(host).split("-")
    return parts[2] if len(parts) > 2 else "Unknown"


def load_noc(path):
    df = pd.read_excel(path, sheet_name="Raw data")
    df["CKT_ID"] = df["Host"].apply(extract_ckt_id)
    df["Site"] = df["Host"].apply(extract_site)
    df["ISP"] = df["Host"].apply(extract_isp)
    df["Time"] = pd.to_datetime(df["Time"])
    df["Recovery time"] = pd.to_datetime(df["Recovery time"])
    return df


def detect_isp_name(filename):
    for pattern, isp in ISP_FILE_MAP.items():
        if pattern in filename:
            return isp
    return None


def load_isp_file(path, isp_name):
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


def load_all_isp_files(docs_dir):
    frames = []
    for path in sorted(glob(str(docs_dir / "*.xlsx"))):
        fname = Path(path).name
        if fname in (NOC_FILE, MASTER_SITE_FILE, Path(OUTPUT_FILE).name):
            continue
        isp_name = detect_isp_name(fname)
        if not isp_name:
            continue
        df = load_isp_file(path, isp_name)
        if not df.empty:
            frames.append(df)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


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


def calculate_overlap_minutes(group):
    """Calculate minutes where BOTH ISPs for a site are down simultaneously."""
    isps = group["ISP"].unique()
    if len(isps) < 2:
        return 0.0
    total = 0.0
    isp1 = group[group["ISP"] == isps[0]][["Time", "Recovery time"]].values
    isp2 = group[group["ISP"] == isps[1]][["Time", "Recovery time"]].values
    for s1, e1 in isp1:
        for s2, e2 in isp2:
            overlap = (min(e1, e2) - max(s1, s2)) / np.timedelta64(1, "s")
            if overlap > 0:
                total += overlap
    return round(total / 60, 2)


def build_sheet1(noc_df, master_sites, site_mapping):
    """Daily SLA sheet: one row per site per day, sorted by Site → Date."""
    report_month = noc_df["Time"].dt.to_period("M").mode()[0]
    all_days = pd.date_range(
        report_month.start_time, periods=report_month.day, freq="D"
    )

    rows = []
    for (date, site), group in noc_df.groupby([noc_df["Time"].dt.floor("D"), "Site"]):
        mapping = site_mapping.get(site, {})
        isp1 = mapping.get("ISP1 Name") or ""
        isp2 = mapping.get("ISP2 Name") or ""
        
        ckt1 = str(mapping.get("ISP1 CKT ID") or "")
        ckt2 = str(mapping.get("ISP2 CKT ID") or "")
        if ckt1 in ["Unknown_Airtel", "Unknown_Jio", "No_CKT", "nan"]: ckt1 = ""
        if ckt2 in ["Unknown_Airtel", "Unknown_Jio", "No_CKT", "nan"]: ckt2 = ""

        # Normalize the group ISPs for downtime calculation
        group_isps = group["ISP"].map(lambda x: ISP_NORMALIZE.get(x, x))
        down1 = group[group_isps == isp1]["Downtime (min)"].sum() if isp1 else 0
        down2 = group[group_isps == isp2]["Downtime (min)"].sum() if isp2 else 0
        
        actual_down = calculate_overlap_minutes(group)
        rows.append(
            {
                "Date": date.strftime("%d-%m-%Y"),
                "Site": site,
                "ISP1 Name": isp1,
                "ISP1 CKT ID": ckt1,
                "ISP1 Down (min)": down1,
                "ISP2 Name": isp2,
                "ISP2 CKT ID": ckt2,
                "ISP2 Down (min)": down2,
                "Actual Site Down (min)": actual_down,
                "Daily SLA %": (1440 - actual_down) / 1440,
            }
        )

    df_result = pd.DataFrame(rows)

    # Zero-downtime injection
    existing = (
        set(zip(df_result["Date"], df_result["Site"])) if not df_result.empty else set()
    )
    
    zero_rows = []
    for d in all_days:
        for s in master_sites:
            if (d.strftime("%d-%m-%Y"), s) not in existing:
                mapping = site_mapping.get(s, {})
                isp1 = mapping.get("ISP1 Name") or ""
                isp2 = mapping.get("ISP2 Name") or ""
                ckt1 = str(mapping.get("ISP1 CKT ID") or "")
                ckt2 = str(mapping.get("ISP2 CKT ID") or "")
                if ckt1 in ["Unknown_Airtel", "Unknown_Jio", "No_CKT", "nan"]: ckt1 = ""
                if ckt2 in ["Unknown_Airtel", "Unknown_Jio", "No_CKT", "nan"]: ckt2 = ""
                
                zero_rows.append(
                    {
                        "Date": d.strftime("%d-%m-%Y"),
                        "Site": s,
                        "ISP1 Name": isp1,
                        "ISP1 CKT ID": ckt1,
                        "ISP1 Down (min)": 0,
                        "ISP2 Name": isp2,
                        "ISP2 CKT ID": ckt2,
                        "ISP2 Down (min)": 0,
                        "Actual Site Down (min)": 0,
                        "Daily SLA %": 1.0,
                    }
                )

    if zero_rows:
        df_result = pd.concat([df_result, pd.DataFrame(zero_rows)], ignore_index=True)

    df_result["_sort"] = pd.to_datetime(df_result["Date"], format="%d-%m-%Y")
    return (
        df_result.sort_values(["Site", "_sort"])
        .drop(columns=["_sort"])
        .reset_index(drop=True)
    )


def build_sheet2(matched_df):
    """Circuit detail sheet: one row per NOC event, sorted by Site → CKT ID."""
    fmt = lambda t: t.strftime("%d-%m-%Y %H:%M:%S") if pd.notna(t) else ""
    rows = [
        {
            "Site": r["Site"],
            "CKT ID": str(r.get("CKT_ID", "") or ""),
            "ISP Name": r["ISP"],
            "Total Downtime (min)": r.get("Downtime (min)", 0),
            "Match Status": r.get("Match_Status", ""),
            "NOC Start": fmt(r["Time"]),
            "NOC End": fmt(r["Recovery time"]),
            "ISP Start": fmt(r.get("ISP_Start")),
            "ISP End": fmt(r.get("ISP_End")),
        }
        for _, r in matched_df.iterrows()
    ]
    df = pd.DataFrame(rows)
    return (
        df.sort_values(["Site", "CKT ID"]).reset_index(drop=True)
        if not df.empty
        else df
    )


def build_minute_sheets(noc_df, master_sites, site_mapping):
    """Minute-by-minute ISP up/down status per site, split into weekly DataFrames."""
    report_month = noc_df["Time"].dt.to_period("M").mode()[0]
    month_start = report_month.start_time
    total_days = report_month.day
    total_minutes = total_days * 1440

    # Build per-site, per-ISP downtime boolean arrays (True = down)
    site_down = {
        site: {isp: np.zeros(total_minutes, dtype=bool) for isp in ISP_COLUMNS}
        for site in master_sites
    }
    for _, r in noc_df.iterrows():
        site, isp_norm = r["Site"], ISP_NORMALIZE.get(r["ISP"], r["ISP"])
        if isp_norm not in site_down.get(site, {}):
            continue
        s = max(0, int((r["Time"] - month_start).total_seconds() // 60))
        e = min(
            total_minutes, int((r["Recovery time"] - month_start).total_seconds() // 60)
        )
        if s < e:
            site_down[site][isp_norm][s:e] = True

    # Generate weekly DataFrames
    weeks, day, week_num = [], 0, 1
    while day < total_days:
        week_days = min(7, total_days - day)
        w_start, w_end = day * 1440, (day + week_days) * 1440
        rows = []
        for site in sorted(master_sites):
            mapping = site_mapping.get(site, {})
            isp1 = mapping.get("ISP1 Name") or ""
            isp2 = mapping.get("ISP2 Name") or ""
            ckt1 = str(mapping.get("ISP1 CKT ID") or "")
            ckt2 = str(mapping.get("ISP2 CKT ID") or "")
            if ckt1 in ["Unknown_Airtel", "Unknown_Jio", "No_CKT", "nan"]: ckt1 = ""
            if ckt2 in ["Unknown_Airtel", "Unknown_Jio", "No_CKT", "nan"]: ckt2 = ""
            
            active = {i for i in [isp1, isp2] if i}
            ckt_str = ",".join(filter(None, [ckt1, ckt2]))
            
            for m in range(w_start, w_end):
                dt = month_start + pd.Timedelta(minutes=m)
                st = {}
                for col in ISP_COLUMNS:
                    st[col] = (
                        ("Down" if site_down[site][col][m] else "Up")
                        if col in active
                        else ""
                    )
                active_down = [st[c] == "Down" for c in ISP_COLUMNS if c in active]
                rows.append(
                    {
                        "Location": site,
                        "CktId": ckt_str,
                        "Date": dt.strftime("%d-%m-%Y"),
                        "Time": dt.strftime("%H:%M"),
                        **{c: st[c] for c in ISP_COLUMNS},
                        "Link": "Down" if all(active_down) and active_down else "Up",
                    }
                )
        weeks.append((f"Week {week_num}", pd.DataFrame(rows)))
        day += week_days
        week_num += 1
    return weeks


def write_report(sheet1, sheet2, weekly_sheets, output_path):
    with pd.ExcelWriter(output_path, engine="xlsxwriter") as writer:
        workbook = writer.book
        pct_fmt = workbook.add_format({"num_format": "0.00%"})
        text_fmt = workbook.add_format({"num_format": "@"})

        # Write sheets
        sheet1.to_excel(writer, index=False, sheet_name="Daily SLA")
        sheet2.to_excel(writer, index=False, sheet_name="Circuit Details")
        for week_name, week_df in weekly_sheets:
            week_df.to_excel(writer, index=False, sheet_name=week_name)

        sheets_meta = [
            ("Daily SLA", sheet1, ["ISP1 CKT ID", "ISP2 CKT ID"], ["Daily SLA %"]),
            ("Circuit Details", sheet2, ["CKT ID"], []),
        ]
        for week_name, week_df in weekly_sheets:
            sheets_meta.append((week_name, week_df, ["CktId"], []))

        for sheet_name, df, txt_cols, pct_cols in sheets_meta:
            worksheet = writer.sheets[sheet_name]

            # Add table
            table_name = sheet_name.replace(" ", "_") + "_Table"  # noqa: F841
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

            # Format and width for each column
            for i, col_name in enumerate(df.columns):
                # Calculate max length efficiently
                max_val_len = (
                    df[col_name].astype(str).map(len).max() if not df.empty else 0
                )
                max_len = max(len(str(col_name)), max_val_len)

                fmt = None
                if col_name in txt_cols:
                    fmt = text_fmt
                elif col_name in pct_cols:
                    fmt = pct_fmt

                worksheet.set_column(i, i, max(max_len + 2, 12), fmt)


def main() -> None:
    noc_path = DOCS_DIR / NOC_FILE
    print(f"Loading NOC data from {noc_path}")
    noc_df = load_noc(noc_path)
    print(
        f"  {len(noc_df)} alerts, {noc_df['Site'].nunique()} sites, ISPs: {sorted(noc_df['ISP'].unique())}"
    )

    print("Loading ISP hardware logs...")
    isp_df = load_all_isp_files(DOCS_DIR)
    print(
        f"  {len(isp_df)} ISP records from: {sorted(isp_df['ISP_Name'].unique())}"
        if not isp_df.empty
        else "  No ISP files found"
    )

    print("Matching NOC alerts → ISP records (overlapping intervals)...")
    matched = match_by_overlap(noc_df, isp_df)
    for status in ["Matched", "NOC Only", "Unmatched"]:
        print(f"  {status}: {(matched['Match_Status'] == status).sum()}")

    master = pd.read_excel(DOCS_DIR / MASTER_SITE_FILE, sheet_name=MASTER_SITE_SHEET)
    master_sites = sorted(master["Site"].dropna().unique())
    print(f"Master site list: {len(master_sites)} sites")

    mapping_path = DOCS_DIR / MASTER_MAPPING_FILE
    if mapping_path.exists():
        site_mapping = pd.read_excel(mapping_path).set_index("Location").to_dict("index")
    else:
        site_mapping = {}

    print("Building reports...")
    sheet1 = build_sheet1(noc_df, master_sites, site_mapping)
    sheet2 = build_sheet2(matched)
    weekly_sheets = build_minute_sheets(noc_df, master_sites, site_mapping)
    write_report(sheet1, sheet2, weekly_sheets, OUTPUT_FILE)
    print(f"Done! → {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
