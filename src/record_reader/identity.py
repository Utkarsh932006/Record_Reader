from __future__ import annotations

import re
from dataclasses import dataclass

from rapidfuzz import process as fuzz_process

from record_reader.config import AppConfig

_CKT_PATTERNS = (
    re.compile(r"CKTID\s*-\s*(\d+)", re.IGNORECASE),
    re.compile(r"CK[TD](?:-ID)?-(\d+)", re.IGNORECASE),
    re.compile(r"Ishan(\d+)", re.IGNORECASE),
)


@dataclass(frozen=True)
class ParsedHost:
    site: str | None
    isp: str | None
    ckt_id: str | None
    source: str


def canonical_ckt(val) -> str:
    """Normalize a circuit id to a digit string (no Excel float tails)."""
    if val is None:
        return ""
    s = str(val).strip()
    if s.lower() in {"", "nan", "none", "nat", "unknown_airtel", "unknown_jio", "no_ckt"}:
        return ""
    if s.endswith(".0"):
        s = s[:-2]
    if "e+" in s.lower() or "e-" in s.lower():
        try:
            s = f"{float(s):.0f}"
        except ValueError:
            pass
    return s.replace(".0", "")


def _alias_site(site: str | None, cfg: AppConfig) -> str | None:
    if not site:
        return site
    lookup = {k.upper(): v for k, v in cfg.site_aliases.items()}
    return lookup.get(site.upper(), site)


def _alias_city(token: str, cfg: AppConfig) -> str:
    lookup = {k.upper(): v for k, v in cfg.city_aliases.items()}
    return lookup.get(token.upper(), token)


def extract_ckt_id(host: str) -> str | None:
    h = str(host)
    for pat in _CKT_PATTERNS:
        match = pat.search(h)
        if match:
            return match.group(1)
    return None


def parse_host(host: str, cfg: AppConfig) -> ParsedHost:
    """Extract (site, isp, ckt_id) from a NOC hostname."""
    h = str(host)

    for rule in cfg.host_contains_overrides:
        needle = str(rule.get("contains") or "")
        if needle and needle.lower() in h.lower():
            ckt = None
            if rule.get("ckt_regex"):
                m = re.search(rule["ckt_regex"], h, re.IGNORECASE)
                ckt = m.group(1) if m else extract_ckt_id(h)
            else:
                ckt = extract_ckt_id(h)
            site = _alias_site(rule.get("site"), cfg)
            return ParsedHost(site=site, isp=rule.get("isp"), ckt_id=ckt, source="override")

    parts = h.split("-")
    if len(parts) >= 3 and parts[1].upper() == "PUNE" and parts[2].upper() == "CDC":
        site = f"{parts[0]}-PUNE-CDC"
        isp = parts[3] if len(parts) > 3 else "Unknown"
    else:
        site = "-".join(parts[:2]) if len(parts) >= 2 else h
        isp = parts[2] if len(parts) > 2 else "Unknown"

    site = _alias_site(site, cfg)
    ckt = extract_ckt_id(h)
    return ParsedHost(site=site, isp=isp, ckt_id=ckt, source="parsed")


def normalize_isp(isp: str | None, cfg: AppConfig) -> str:
    if not isp:
        return ""
    return cfg.isp_normalize.get(isp, cfg.isp_normalize.get(isp.upper(), isp))


def _master_lookup(master_sites: list[str]) -> dict[str, str]:
    return {s.upper(): s for s in master_sites}


def fuzzy_match_site(host: str, master_sites: list[str], cfg: AppConfig) -> str | None:
    """Resolve a firewall hostname to a master site name."""
    if not master_sites:
        return None
    host_str = str(host)
    by_upper = _master_lookup(master_sites)

    alias = cfg.firewall_host_aliases.get(host_str) or cfg.firewall_host_aliases.get(
        host_str.upper()
    )
    if alias:
        return by_upper.get(alias.upper(), alias)

    for alias_from, canonical in cfg.city_aliases.items():
        host_str = re.sub(alias_from, canonical, host_str, flags=re.IGNORECASE)

    match = re.search(
        r"([A-Za-z][A-Za-z-]*?)-(Branch|Warehouse|Zonal Branch)-(?:FW-?)?",
        host_str,
        re.IGNORECASE,
    )
    if match:
        city = _alias_city(match.group(1).upper(), cfg)
        typ = match.group(2).upper()
        prefix = "WH" if "WAREHOUSE" in typ else "BR"
        site = f"{prefix}-{city}"
        if site.upper() in by_upper:
            return by_upper[site.upper()]
        resolved = _fuzzy_city(city, prefix, master_sites, cfg)
        if resolved:
            return resolved

    # Chandigarh-Zirakpur-Branch → BR-ZIRAKPUR_CHANDIGHAR
    parts = host_str.split("-")
    if len(parts) > 1:
        joined = "_".join(p.upper() for p in parts if p.upper() not in {"BRANCH", "WAREHOUSE", "ZONAL", "FW"})
        for ms in master_sites:
            token = ms.split("-", 1)[-1].upper().replace("-", "_")
            if token and token in joined:
                return ms

    parts = host_str.split("-")
    if len(parts) > 1:
        city = _alias_city(parts[0].upper(), cfg)
        has_warehouse = any("WAREHOUSE" in p.upper() for p in parts)
        prefix = "WH" if has_warehouse else "BR"
        return _fuzzy_city(city, prefix, master_sites, cfg)
    return None


def _fuzzy_city(
    city: str, prefix: str, master_sites: list[str], cfg: AppConfig
) -> str | None:
    city_parts = [ms.split("-", 1)[-1] for ms in master_sites]
    if not city_parts:
        return None
    result = fuzz_process.extractOne(city, city_parts, score_cutoff=cfg.fuzzy_score_cutoff)
    if not result:
        return None
    best, _score, _ = result
    for ms in master_sites:
        if ms.upper() == f"{prefix}-{best}".upper():
            return ms
    for ms in master_sites:
        if best.upper() in ms.upper():
            return ms
    return None
