import pandas as pd
import numpy as np


def calculate_overlap_minutes(group_df):
    isps = group_df["ISP"].unique()
    if len(isps) < 2:
        return 0

    isp1_intervals = group_df[group_df["ISP"] == isps[0]][
        ["Time", "Recovery time"]
    ].values
    isp2_intervals = group_df[group_df["ISP"] == isps[1]][
        ["Time", "Recovery time"]
    ].values

    overlap_seconds = 0
    for s1, e1 in isp1_intervals:
        for s2, e2 in isp2_intervals:
            start_max = max(s1, s2)
            end_min = min(e1, e2)

            if start_max < end_min:
                overlap_seconds += (end_min - start_max) / np.timedelta64(1, "s")

    return round(overlap_seconds / 60)


def generate_sla_report(input_file, output_file):
    print(f"Loading raw data from {input_file}...")
    df_raw = pd.read_excel(input_file, sheet_name="Raw data")

    df_raw["Site"] = df_raw["Host"].apply(lambda x: "-".join(str(x).split("-")[:2]))
    df_raw["ISP"] = df_raw["Host"].apply(
        lambda x: str(x).split("-")[2] if len(str(x).split("-")) > 2 else "Unknown"
    )

    df_raw["Date"] = pd.to_datetime(df_raw["Time"]).dt.floor("D")

    results = []

    for (date, site), group in df_raw.groupby(["Date", "Site"]):
        isps = group["ISP"].unique()

        isp1_down = (
            group[group["ISP"] == isps[0]]["Downtime (min)"].sum()
            if len(isps) > 0
            else 0
        )
        isp2_down = (
            group[group["ISP"] == isps[1]]["Downtime (min)"].sum()
            if len(isps) > 1
            else 0
        )

        actual_down = calculate_overlap_minutes(group)

        daily_sla = (1440 - actual_down) / 1440

        results.append(
            {
                "Date": date,
                "Site": site,
                "ISP1 Down (min)": isp1_down,
                "ISP2 Down (min)": isp2_down,
                "Actual Site Down (min)": actual_down,
                "Daily SLA %": daily_sla,
            }
        )

    df_isp_generated = pd.DataFrame(results)

    df_isp_generated = df_isp_generated.sort_values(by=["Date", "Site"]).reset_index(
        drop=True
    )

    df_isp_generated["Date"] = df_isp_generated["Date"].dt.strftime("%Y-%m-%d")

    print(f"Saving generated SLA report to {output_file}...")
    df_isp_generated.to_excel(output_file, index=False, sheet_name="ISP_Generated")
    print("Done!")


def main() -> None:
    generate_sla_report("ISP(1).xlsx", "Generated_ISP_Report.xlsx")


if __name__ == "__main__":
    main()
