from __future__ import annotations

import logging
import re
from glob import glob
from pathlib import Path

import pandas as pd

from record_reader.config import AppConfig
from record_reader.identity import (
    canonical_ckt,
    fuzzy_match_site,
    normalize_isp,
    parse_host,
)
from record_reader.intervals import clip_interval, interval_minutes

log = logging.getLogger("record_reader")

INVALID_CKT = {"", "Unknown_Airtel", "Unknown_Jio", "No_CKT", "nan", "None"}


def find_file(docs_dir: Path, pattern: str, exclude: set[str] | None = None) -> Path | None:
    exclude = set(exclude or [])
    for path in sorted(glob(str(docs_dir / pattern))):
        if Path(path).name not in exclude:
            return Path(path)
    return None


def parse_duration_to_minutes(duration_str) -> float:
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


def _clean_mapping_ckt(val) -> str:
    s = canonical_ckt(val)
    return "" if s in INVALID_CKT else s


def load_master_mapping(path: Path) -> tuple[dict, list[str]]:
    """Load site inventory. Circuit IDs are forced to strings."""
    if not path.exists():
        log.warning("Master mapping not found: %s", path)
        return {}, []
    df = pd.read_excel(path, dtype=str)
    df.columns = [str(c).strip() for c in df.columns]
    if "Location" not in df.columns:
        raise ValueError(f"Master mapping missing Location column: {list(df.columns)}")
    df["Location"] = df["Location"].astype(str).str.strip()
    for col in ("ISP1 CKT ID", "ISP2 CKT ID"):
        if col in df.columns:
            df[col] = df[col].map(_clean_mapping_ckt)
    site_mapping = df.set_index("Location").to_dict("index")
    return site_mapping, sorted(site_mapping.keys())


def ckt_to_sites(site_mapping: dict) -> dict[str, list[str]]:
    index: dict[str, list[str]] = {}
    for site, row in site_mapping.items():
        for key in ("ISP1 CKT ID", "ISP2 CKT ID"):
            ckt = _clean_mapping_ckt(row.get(key))
            if ckt:
                index.setdefault(ckt, []).append(site)
    return index


def _apply_event_window(
    df: pd.DataFrame,
    start_col: str,
    end_col: str,
    window_start: pd.Timestamp | None,
    window_end: pd.Timestamp | None,
    flap_min: float,
) -> pd.DataFrame:
    if df.empty:
        return df
    records = []
    for _, row in df.iterrows():
        clipped = clip_interval(row[start_col], row[end_col], window_start, window_end)
        if clipped is None:
            continue
        mins = interval_minutes([clipped])
        if mins < flap_min:
            continue
        new = row.copy()
        new[start_col] = clipped[0]
        new[end_col] = clipped[1]
        new["Duration_min"] = mins
        records.append(new)
    if not records:
        empty = df.iloc[0:0].copy()
        if "Duration_min" not in empty.columns:
            empty["Duration_min"] = pd.Series(dtype=float)
        return empty
    return pd.DataFrame(records).reset_index(drop=True)


def load_noc(
    path: Path,
    cfg: AppConfig,
    window_start: pd.Timestamp | None = None,
    window_end: pd.Timestamp | None = None,
) -> pd.DataFrame:
    df = pd.read_excel(path, sheet_name=cfg.noc_sheet)
    parsed = df["Host"].map(lambda h: parse_host(h, cfg))
    df["CKT_ID"] = [p.ckt_id or "" for p in parsed]
    df["Site"] = [p.site for p in parsed]
    df["ISP"] = [p.isp for p in parsed]
    df["Time"] = pd.to_datetime(df["Time"])
    df["Recovery time"] = pd.to_datetime(df["Recovery time"])
    df["_ISP_Norm"] = df["ISP"].map(lambda x: normalize_isp(x, cfg))
    df["Duration_min"] = df["Duration"].map(parse_duration_to_minutes)
    df["_parse_source"] = [p.source for p in parsed]
    df = _apply_event_window(
        df, "Time", "Recovery time", window_start, window_end, cfg.flap_min_minutes
    )
    return df


def load_firewall(
    path: Path,
    master_sites: list[str],
    cfg: AppConfig,
    window_start: pd.Timestamp | None = None,
    window_end: pd.Timestamp | None = None,
) -> pd.DataFrame:
    df = pd.read_excel(path, sheet_name=cfg.firewall_sheet)
    df["Site"] = df["Host"].map(lambda x: fuzzy_match_site(x, master_sites, cfg))
    df["Time"] = pd.to_datetime(df["Time"])
    df["Recovery time"] = pd.to_datetime(df["Recovery time"])
    df["Duration_min"] = df["Duration"].map(parse_duration_to_minutes)
    df = _apply_event_window(
        df, "Time", "Recovery time", window_start, window_end, cfg.flap_min_minutes
    )
    return df


def detect_isp_name(filename: str, cfg: AppConfig) -> str | None:
    for pattern, isp in cfg.isp_file_map.items():
        if pattern in filename:
            return isp
    return None


def load_isp_file(path: Path, isp_name: str, cfg: AppConfig) -> pd.DataFrame:
    col_map = cfg.isp_column_map.get(isp_name)
    if not col_map:
        return pd.DataFrame()

    if col_map.sheet:
        df = pd.read_excel(path, sheet_name=col_map.sheet, dtype=str)
    else:
        for sheet in pd.ExcelFile(path).sheet_names:
            df = pd.read_excel(path, sheet_name=sheet, dtype=str)
            if col_map.circuit_id in df.columns and col_map.start in df.columns:
                break
        else:
            return pd.DataFrame()

    cid, start, end = col_map.circuit_id, col_map.start, col_map.end
    if not all(c in df.columns for c in [cid, start, end]):
        return pd.DataFrame()

    df = df.dropna(subset=[cid, start, end])
    df = df.rename(columns={cid: "CKT_ID", start: "ISP_Start", end: "ISP_End"})
    df["CKT_ID"] = df["CKT_ID"].map(canonical_ckt)
    df["ISP_Start"] = pd.to_datetime(df["ISP_Start"])
    df["ISP_End"] = pd.to_datetime(df["ISP_End"])
    df["ISP_Name"] = isp_name
    return df[["CKT_ID", "ISP_Start", "ISP_End", "ISP_Name"]]


def load_all_isp_files(docs_dir: Path, cfg: AppConfig, exclude_names: set[str] | None = None) -> pd.DataFrame:
    exclude = set(exclude_names or [])
    frames = []
    for path in sorted(glob(str(docs_dir / "*.xlsx"))):
        fname = Path(path).name
        if fname in exclude:
            continue
        isp_name = detect_isp_name(fname, cfg)
        if not isp_name:
            continue
        df = load_isp_file(Path(path), isp_name, cfg)
        if not df.empty:
            frames.append(df)
            log.info("Loaded %s vendor rows from %s", len(df), fname)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
