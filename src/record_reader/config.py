from __future__ import annotations

from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class IspColumnMap:
    circuit_id: str
    start: str
    end: str
    sheet: str | None = None


@dataclass
class AppConfig:
    table_style: str = "TableStyleLight9"
    master_mapping_file: str = "Site_ISP_Master_Mapping.xlsx"
    output_file: str = "Generated_ISP_Report.xlsx"
    noc_pattern: str = "Haier ISP *.xlsx"
    firewall_pattern: str = "Haier Firewall *.xlsx"
    noc_sheet: str = "Row data "
    firewall_sheet: str = "Row data "
    overlap_slack_minutes: float = 5.0
    flap_min_minutes: float = 0.0
    clip_to_report_month: bool = True
    fuzzy_score_cutoff: int = 80
    isp_file_map: dict[str, str] = field(default_factory=dict)
    isp_column_map: dict[str, IspColumnMap] = field(default_factory=dict)
    isp_normalize: dict[str, str] = field(default_factory=dict)
    isp_columns: list[str] = field(default_factory=list)
    site_aliases: dict[str, str] = field(default_factory=dict)
    city_aliases: dict[str, str] = field(default_factory=dict)
    firewall_host_aliases: dict[str, str] = field(default_factory=dict)
    host_contains_overrides: list[dict[str, Any]] = field(default_factory=list)

    @property
    def known_vendor_isps(self) -> set[str]:
        """Vendor names in the same canonical form used by NOC events."""
        return {
            self.isp_normalize.get(name, self.isp_normalize.get(name.upper(), name))
            for name in self.isp_file_map.values()
        }


def default_config_path() -> Path:
    return Path(str(resources.files("record_reader").joinpath("config.yaml")))


def load_config(path: str | Path | None = None) -> AppConfig:
    cfg_path = Path(path) if path else default_config_path()
    raw = yaml.safe_load(cfg_path.read_text()) or {}
    col_map = {}
    for isp, spec in (raw.get("isp_column_map") or {}).items():
        col_map[isp] = IspColumnMap(
            circuit_id=spec["circuit_id"],
            start=spec["start"],
            end=spec["end"],
            sheet=spec.get("sheet"),
        )
    return AppConfig(
        table_style=raw.get("table_style", "TableStyleLight9"),
        master_mapping_file=raw.get("master_mapping_file", "Site_ISP_Master_Mapping.xlsx"),
        output_file=raw.get("output_file", "Generated_ISP_Report.xlsx"),
        noc_pattern=raw.get("noc_pattern", "Haier ISP *.xlsx"),
        firewall_pattern=raw.get("firewall_pattern", "Haier Firewall *.xlsx"),
        noc_sheet=raw.get("noc_sheet", "Row data "),
        firewall_sheet=raw.get("firewall_sheet", "Row data "),
        overlap_slack_minutes=float(raw.get("overlap_slack_minutes", 5)),
        flap_min_minutes=float(raw.get("flap_min_minutes", 0)),
        clip_to_report_month=bool(raw.get("clip_to_report_month", True)),
        fuzzy_score_cutoff=int(raw.get("fuzzy_score_cutoff", 80)),
        isp_file_map=dict(raw.get("isp_file_map") or {}),
        isp_column_map=col_map,
        isp_normalize=dict(raw.get("isp_normalize") or {}),
        isp_columns=list(raw.get("isp_columns") or []),
        site_aliases=dict(raw.get("site_aliases") or {}),
        city_aliases=dict(raw.get("city_aliases") or {}),
        firewall_host_aliases=dict(raw.get("firewall_host_aliases") or {}),
        host_contains_overrides=list(raw.get("host_contains_overrides") or []),
    )
