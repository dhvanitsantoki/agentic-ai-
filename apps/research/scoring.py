"""OEM opportunity scoring (0-100) and A–E categorisation.

Points are awarded only for signals that the research agent backed with a quote AND a
source URL that was reachable during verification. The breakdown is stored on the company
so every score can be explained.
"""
from apps.crm.models import CompanyType, OemCategory

SIGNALS = [
    ("sells_valves_own_brand", "Sells valves / irrigation products under its own brand", 25),
    ("outsources_manufacturing", "Evidence of outsourcing / sourcing from contract manufacturers", 15),
    ("imports_plastic_valves", "Imports plastic valves or fittings", 15),
    ("manufactures_irrigation", "Manufactures irrigation or plumbing products", 12),
    ("private_label_need", "Private-label / custom-design need", 10),
    ("distribution_network", "Has its own distribution network / branches / dealers", 10),
    ("bulk_volume", "Indicators of regular bulk volumes (projects, large range, many outlets)", 8),
    ("product_fit", "Product range overlaps Flotec categories (PVC/PP/CPVC/UPVC valves, drip irrigation)", 10),
]
OEM_SIGNAL_KEYS = {"sells_valves_own_brand", "outsources_manufacturing", "private_label_need"}
DISTRIBUTION_TYPES = {CompanyType.DISTRIBUTOR, CompanyType.WHOLESALER, CompanyType.HARDWARE, CompanyType.RETAILER,
                      CompanyType.PLUMBING, CompanyType.CONTRACTOR}


def score_signals(signals: dict, reachable_urls: set[str], quote_checker=None) -> dict:
    """Return {"score": int, "signals": [...]} counting only evidence-backed signals."""
    total = 0
    breakdown = []
    for key, label, points in SIGNALS:
        s = (signals or {}).get(key) or {}
        claimed = bool(s.get("present"))
        evidence = (s.get("evidence") or "").strip()
        url = (s.get("source_url") or "").strip()
        backed = claimed and bool(evidence) and bool(url) and url in reachable_urls
        quote_found = bool(quote_checker(evidence, url)) if (backed and quote_checker) else None
        counted = backed
        breakdown.append({
            "key": key, "label": label, "points": points, "claimed": claimed, "present": counted,
            "evidence": evidence[:400], "source_url": url, "quote_found": quote_found,
        })
        if counted:
            total += points
    return {"score": min(total, 100), "signals": breakdown}


def categorise(score: int, company_type: str, relevance: str, breakdown: dict) -> str:
    present = {s["key"] for s in breakdown.get("signals", []) if s["present"]}
    if relevance == "none":
        return OemCategory.E
    oem_evidence = bool(present & OEM_SIGNAL_KEYS)
    if score >= 60 and oem_evidence:
        return OemCategory.A
    if score >= 40 and (oem_evidence or "manufactures_irrigation" in present):
        return OemCategory.B
    if company_type == CompanyType.IMPORTER or "imports_plastic_valves" in present:
        return OemCategory.D
    if company_type in DISTRIBUTION_TYPES or "distribution_network" in present:
        return OemCategory.C
    if score >= 25:
        return OemCategory.C
    return OemCategory.E
