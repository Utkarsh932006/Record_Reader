"""Command-line entry point for generating ISP availability reports."""

from __future__ import annotations

import argparse
import logging
import re
from pathlib import Path

import pandas as pd

from record_reader.config import AppConfig, load_config
from record_reader.excel_out import write_report
from record_reader.loaders import (
    ckt_to_sites,
    find_file,
    load_all_isp_files,
    load_firewall,
    load_master_mapping,
    load_noc,
)
from record_reader.matching import match_by_overlap
from record_reader.reports import (
    build_circuit_details,
    build_daily_sla,
    build_data_quality,
    build_firewall_details,
    build_minute_sheets,
    build_summary,
    report_month_bounds,
)

log = logging.getLogger("record_reader")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate an ISP availability report from Excel exports.",
    )
    parser.add_argument(
        "--docs-dir",
        type=Path,
        default=Path.cwd() / "Docs",
        help="Directory containing the Excel input files (default: ./Docs).",
    )
    parser.add_argument(
        "--config",
        type=Path,
        help="Path to a YAML configuration file.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Report path. Relative paths are resolved inside --docs-dir.",
    )
    parser.add_argument(
        "--month",
        metavar="YYYY-MM",
        help="Report month; inferred from the alerts when omitted.",
    )
    parser.add_argument(
        "--minute-sheets",
        action="store_true",
        help="Include the large per-minute weekly detail sheets.",
    )
    parser.add_argument("--verbose", action="store_true", help="Show diagnostic logs.")
    return parser


def _report_month(value: str | None) -> pd.Period | None:
    if value is None:
        return None
    if not re.fullmatch(r"\d{4}-\d{2}", value):
        raise ValueError("--month must use YYYY-MM")
    try:
        p = pd.Period(value, freq="M")
        return p if isinstance(p, pd.Period) else None
    except ValueError as exc:
        raise ValueError("--month must use YYYY-MM") from exc


def _resolve_output(path: Path | None, docs_dir: Path, cfg: AppConfig) -> Path:
    output = path or Path(cfg.output_file)
    return output if output.is_absolute() else docs_dir / output


def run(args: argparse.Namespace) -> Path:
    """Generate a report and return the output path."""
    cfg = load_config(args.config)
    docs_dir = args.docs_dir.expanduser().resolve()
    if not docs_dir.is_dir():
        raise FileNotFoundError(f"Input directory does not exist: {docs_dir}")

    mapping_path = docs_dir / cfg.master_mapping_file
    site_mapping, master_sites = load_master_mapping(mapping_path)
    if not master_sites:
        raise FileNotFoundError(f"Master mapping is missing or empty: {mapping_path}")

    output_path = _resolve_output(args.output, docs_dir, cfg)
    excluded = {mapping_path.name, output_path.name}
    noc_path = find_file(docs_dir, cfg.noc_pattern, excluded)
    if noc_path:
        excluded.add(noc_path.name)
    firewall_path = find_file(docs_dir, cfg.firewall_pattern, excluded)
    if firewall_path:
        excluded.add(firewall_path.name)

    noc_df = load_noc(noc_path, cfg) if noc_path else pd.DataFrame()
    fw_df = (
        load_firewall(firewall_path, master_sites, cfg)
        if firewall_path
        else pd.DataFrame()
    )
    selected_month = _report_month(args.month)
    report_month, window_start, window_end = report_month_bounds(
        noc_df,
        fw_df,
        selected_month,
    )
    if report_month is None:
        raise ValueError("No alert data found to infer a report month; pass --month.")

    if cfg.clip_to_report_month:
        if noc_path:
            noc_df = load_noc(noc_path, cfg, window_start, window_end)
        if firewall_path:
            fw_df = load_firewall(
                firewall_path,
                master_sites,
                cfg,
                window_start,
                window_end,
            )

    isp_df = load_all_isp_files(docs_dir, cfg, excluded)
    matched = match_by_overlap(noc_df, isp_df, cfg, ckt_to_sites(site_mapping))
    daily_sla = build_daily_sla(noc_df, fw_df, master_sites, site_mapping, report_month)
    sheets: list[tuple[str, pd.DataFrame, list[str], list[str]]] = [
        ("Summary", build_summary(daily_sla, fw_df, noc_df), [], ["Monthly SLA %"]),
        (
            "Daily SLA",
            daily_sla,
            ["ISP1 CKT ID", "ISP2 CKT ID"],
            ["Daily SLA %"],
        ),
        ("Circuit Details", build_circuit_details(matched), ["CKT ID"], []),
        ("Firewall Details", build_firewall_details(fw_df), [], []),
        (
            "Data Quality",
            build_data_quality(noc_df, fw_df, matched, master_sites, site_mapping, cfg),
            [],
            [],
        ),
    ]
    if args.minute_sheets:
        sheets.extend(
            (name, frame, ["CktId"], [])
            for name, frame in build_minute_sheets(
                noc_df,
                fw_df,
                master_sites,
                site_mapping,
                cfg,
                report_month,
            )
        )
    write_report(sheets, output_path, cfg.table_style)
    return output_path


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    logging.basicConfig(
        format="%(levelname)s: %(message)s",
        level=logging.INFO if args.verbose else logging.WARNING,
    )
    try:
        output_path = run(args)
    except (FileNotFoundError, ValueError, OSError) as exc:
        log.error("%s", exc)
        return 1
    print(f"Report written to {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
