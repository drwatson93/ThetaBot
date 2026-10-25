"""Sector concentration cap. The loss circuit breaker is gone."""
from types import SimpleNamespace

from agentic.config import RiskConfig
from agentic.services.risk_breaker import apply_sector_cap



def _appr(underlying, collateral):
    return SimpleNamespace(candidate=SimpleNamespace(underlying=underlying), collateral=collateral)


def _open_put(underlying, strike, qty):
    return SimpleNamespace(option_type=SimpleNamespace(value="put"),
                           underlying=underlying, strike=strike, quantity=qty)


_ACCT = 10_000.0


def test_sector_cap_off_keeps_all():
    kept, skipped = apply_sector_cap([_appr("SMR", 5000), _appr("CRWV", 6000)], [], RiskConfig(), _ACCT)
    assert len(kept) == 2 and skipped == []           # max_pct_per_sector None -> fail-open


def test_sector_cap_drops_overflow_in_same_sector():
    cfg = RiskConfig(max_pct_per_sector=0.30, sector_map={"SMR": "nuclear", "OKLO": "nuclear"})
    # cap = 3000; first nuclear (2000) fits, second (2000) would make 4000 > 3000 -> dropped
    kept, skipped = apply_sector_cap([_appr("SMR", 2000), _appr("OKLO", 2000)], [], cfg, _ACCT)
    assert [e.candidate.underlying for e in kept] == ["SMR"]
    assert len(skipped) == 1 and "nuclear" in skipped[0][1]


def test_sector_cap_allows_across_sectors():
    cfg = RiskConfig(max_pct_per_sector=0.30, sector_map={"SMR": "nuclear", "SOFI": "fintech"})
    kept, _ = apply_sector_cap([_appr("SMR", 2500), _appr("SOFI", 2500)], [], cfg, _ACCT)
    assert len(kept) == 2                              # different sectors, each under the cap


def test_sector_cap_counts_existing_open_puts():
    cfg = RiskConfig(max_pct_per_sector=0.30, sector_map={"SMR": "nuclear", "OKLO": "nuclear"})
    opens = [_open_put("SMR", 25.0, 1)]                # 25*100*1 = 2500 already in 'nuclear'
    kept, skipped = apply_sector_cap([_appr("OKLO", 1000)], opens, cfg, _ACCT)  # 2500+1000 > 3000
    assert kept == [] and len(skipped) == 1


def test_sector_cap_unmapped_names_are_singletons():
    # no sector_map -> each name its own sector -> both fit (2000 < 3000)
    kept, _ = apply_sector_cap([_appr("AAA", 2000), _appr("BBB", 2000)], [],
                               RiskConfig(max_pct_per_sector=0.30), _ACCT)
    assert len(kept) == 2
