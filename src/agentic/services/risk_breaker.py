"""Sector concentration for the CSP sizer and the dashboard.

The loss circuit breaker is gone. These two functions only group short-put collateral.
"""
from __future__ import annotations


def sector_exposure(open_positions, sector_map) -> dict[str, float]:
    """Short-put collateral (strike*100*qty) grouped by sector. Shared by apply_sector_cap and the
    dashboard concentration read so the two never drift. Assumes the caller passes OPEN positions."""
    smap = sector_map or {}

    def sector(sym: str) -> str:
        return smap.get(sym.upper()) or smap.get(sym) or sym.upper()

    exp: dict[str, float] = {}
    for p in open_positions:
        ot = getattr(p, "option_type", None)
        if str(getattr(ot, "value", ot)).lower() == "put":
            coll = (getattr(p, "strike", 0) or 0) * 100 * (getattr(p, "quantity", 0) or 0)
            exp[sector(p.underlying)] = exp.get(sector(p.underlying), 0.0) + coll
    return exp


def apply_sector_cap(approved, open_positions, cfg, account_value):
    """Drop approved CSP entries that would push a sector over ``max_pct_per_sector`` of account value.

    Prevents a book of many names from concentrating into one correlated theme (the failure mode
    where 20 'diversified' positions are really one AI/nuclear/crypto bet). Entries are considered in
    ranked order (``approved`` is already ranked best-first), so the richest opportunities in a sector
    fill first and the marginal ones are dropped once the sector is full. Sector exposure counts
    existing short-put collateral. Fail-open: no cap / no account value -> keep everything.

    Returns ``(kept, skipped)`` where ``skipped`` is a list of ``(candidate, reason)`` mirroring the
    sizer's rejected shape, so the scanner can log them as negative examples.
    """
    cap = cfg.max_pct_per_sector
    if not cap or not account_value or account_value <= 0:
        return list(approved), []
    smap = cfg.sector_map or {}

    def sector(sym: str) -> str:
        return smap.get(sym.upper()) or smap.get(sym) or sym.upper()

    exposure = sector_exposure(open_positions, smap)

    limit = cap * account_value
    kept, skipped = [], []
    for e in approved:
        sec = sector(e.candidate.underlying)
        if exposure.get(sec, 0.0) + e.collateral > limit:
            skipped.append((e.candidate,
                            f"sector cap: '{sec}' would exceed {cap:.0%} of account"))
        else:
            exposure[sec] = exposure.get(sec, 0.0) + e.collateral
            kept.append(e)
    return kept, skipped
