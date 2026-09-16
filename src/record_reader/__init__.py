import re
from glob import glob
from pathlib import Path

import numpy as np
import pandas as pd
from openpyxl.styles import Alignment
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.table import Table, TableStyleInfo

# === USER CONFIG ===
DOCS_DIR = Path(__file__).resolve().parent.parent.parent / "Docs"
OUTPUT_FILE = DOCS_DIR / "Generated_ISP_Report.xlsx"
NOC_FILE = "ISP.xlsx"
MASTER_SITE_FILE = "Copy of ISP (003).xlsx"
MASTER_SITE_SHEET = "ISP"
TABLE_STYLE = "TableStyleLight9"  # e.g. TableStyleMedium2, TableStyleMedium9, TableStyleLight9

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


def build_sheet1(noc_df, master_sites):
    """Daily SLA sheet: one row per site per day, sorted by Site → Date."""
    report_month = noc_df["Time"].dt.to_period("M").mode()[0]
    all_days = pd.date_range(
        report_month.start_time, periods=report_month.day, freq="D"
    )

    rows = []
    for (date, site), group in noc_df.groupby([noc_df["Time"].dt.floor("D"), "Site"]):
        isps = group["ISP"].unique()
        isp1, isp2 = (
            (isps[0] if len(isps) > 0 else ""),
            (isps[1] if len(isps) > 1 else ""),
        )
        ckt1 = str(group[group["ISP"] == isp1]["CKT_ID"].iloc[0] or "") if isp1 else ""
        ckt2 = str(group[group["ISP"] == isp2]["CKT_ID"].iloc[0] or "") if isp2 else ""
        down1 = group[group["ISP"] == isp1]["Downtime (min)"].sum() if isp1 else 0
        down2 = group[group["ISP"] == isp2]["Downtime (min)"].sum() if isp2 else 0
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
    zero_rows = [
        {
            "Date": d.strftime("%d-%m-%Y"),
            "Site": s,
            "ISP1 Name": "",
            "ISP1 CKT ID": "",
            "ISP1 Down (min)": 0,
            "ISP2 Name": "",
            "ISP2 CKT ID": "",
            "ISP2 Down (min)": 0,
            "Actual Site Down (min)": 0,
            "Daily SLA %": 1.0,
        }
        for d in all_days
        for s in master_sites
        if (d.strftime("%d-%m-%Y"), s) not in existing
    ]

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


def write_report(sheet1, sheet2, output_path):
    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        sheet1.to_excel(writer, index=False, sheet_name="Daily SLA")
        sheet2.to_excel(writer, index=False, sheet_name="Circuit Details")

        sheets_meta = [
            (writer.sheets["Daily SLA"], sheet1, "Daily_SLA_Table"),
            (writer.sheets["Circuit Details"], sheet2, "Circuit_Details_Table"),
        ]

        # Format SLA as percentage
        ws1 = writer.sheets["Daily SLA"]
        sla_col = sheet1.columns.get_loc("Daily SLA %") + 1
        for row in range(2, ws1.max_row + 1):
            ws1.cell(row=row, column=sla_col).number_format = "0.00%"

        # Format CKT ID columns as text
        for ws, cols in [
            (ws1, ["ISP1 CKT ID", "ISP2 CKT ID"]),
            (writer.sheets["Circuit Details"], ["CKT ID"]),
        ]:
            for col_name in cols:
                df = sheet1 if ws == ws1 else sheet2
                if col_name not in df.columns:
                    continue
                col_idx = df.columns.get_loc(col_name) + 1
                for row in range(2, ws.max_row + 1):
                    ws.cell(row=row, column=col_idx).number_format = "@"

        # Apply Table style, auto-fit column widths and row heights
        for ws, df, table_name in sheets_meta:
            max_col_letter = get_column_letter(ws.max_column)
            table = Table(displayName=table_name, ref=f"A1:{max_col_letter}{ws.max_row}")
            table.tableStyleInfo = TableStyleInfo(
                name=TABLE_STYLE,
                showFirstColumn=False,
                showLastColumn=False,
                showRowStripes=True,
                showColumnStripes=False,
            )
            ws.add_table(table)

            ws.row_dimensions[1].height = 24
            for r in range(2, ws.max_row + 1):
                ws.row_dimensions[r].height = 19

            for col in ws.columns:
                col_letter = get_column_letter(col[0].column)
                header_str = str(col[0].value or "")
                max_len = len(header_str)
                for cell in col[1:]:
                    if cell.value is not None:
                        val_str = (
                            f"{cell.value * 100:.2f}%"
                            if cell.number_format == "0.00%" and isinstance(cell.value, (int, float))
                            else str(cell.value)
                        )
                        max_len = max(max_len, len(val_str))
                ws.column_dimensions[col_letter].width = max(max_len + 4, 12)


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

    print("Building reports...")
    sheet1 = build_sheet1(noc_df, master_sites)
    sheet2 = build_sheet2(matched)
    write_report(sheet1, sheet2, OUTPUT_FILE)
    print(f"Done! → {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
