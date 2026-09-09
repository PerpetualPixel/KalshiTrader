"""Player form from open tennis data."""
import time

import pytest

from kalshitrader.kalshi.models import Market
from kalshitrader.markets.contest import Contest, Leg
from kalshitrader.tennis.form import FormAnalyst, FormStore, normalise, surname

HEADER = "tourney_date,surface,winner_name,loser_name,score,winner_rank_points,loser_rank_points"


def csv_rows(*rows: str) -> str:
    return "\n".join([HEADER, *rows])


def store_with(rows: str, **kw) -> FormStore:
    st = FormStore(cache_dir="/tmp/kb-form-test", **kw)
    st._index(rows)
    for rec in st.players.values():
        rec.matches.sort(key=lambda r: r["date"], reverse=True)
    st.loaded_at = time.time()
    return st


def make_match(a: str, b: str, tournament: str = "ATP Challenger Shanghai Hard") -> Contest:
    m = Market.from_api({"ticker": "T", "status": "active"})
    return Contest(key="k", a=a, b=b, event=tournament, markets=[m],
                       legs={a: Leg(a, "T", "yes", m), b: Leg(b, "T", "no", m)})


def test_name_normalisation_handles_accents_and_punctuation():
    assert normalise("Ángel Díaz-Núñez") == "angel diaz nunez"
    assert normalise("O'Connell, Christopher") == "o connell christopher"
    assert surname("Rei Sakamoto") == "sakamoto"


def test_parses_records_and_derives_form():
    rows = csv_rows(
        "20260901,Hard,Ana One,Bea Two,6-4 6-4,900,400",
        "20260902,Hard,Ana One,Bea Two,7-6 3-6 6-4,900,400",
        "20260903,Clay,Bea Two,Ana One,6-0 6-0,400,900",
        "20260904,Hard,Ana One,Cara Three,6-2 2-6 6-3,900,300",
    )
    st = store_with(rows)
    ana = st.find("Ana One")
    assert ana.played == 4 and ana.win_rate() == 0.75
    assert ana.surface_win_rate("Hard") == 1.0 and ana.surface_win_rate("Clay") == 0.0
    assert ana.decider_win_rate() == 1.0          # both three-setters won
    assert ana.head_to_head("Bea Two") == (2, 1)
    assert ana.rank_points() == 900


def test_retirement_is_flagged_for_the_player_who_retired():
    st = store_with(csv_rows("20260905,Hard,Ana One,Bea Two,6-2 2-1 RET,900,400"))
    assert st.find("Bea Two").retired_recently() is True
    assert st.find("Ana One").retired_recently() is False


def test_assessment_favours_the_stronger_player():
    rows = csv_rows(*[f"202609{i:02d},Hard,Ana One,Bea Two,6-4 6-4,900,400" for i in range(1, 9)])
    analyst = FormAnalyst(store=store_with(rows))
    a = analyst.assess(make_match("Ana One", "Bea Two"))
    assert a is not None
    assert a.p_a_wins > 0.6 and a.a.form_score > a.b.form_score
    assert 0 < a.confidence <= 0.6, "historical form is a prior, never a certainty"
    assert "in last" in " ".join(a.a.key_facts)


def test_no_assessment_when_a_player_is_unknown_or_thin():
    rows = csv_rows("20260901,Hard,Ana One,Bea Two,6-4 6-4,900,400")
    analyst = FormAnalyst(store=store_with(rows), min_matches=3)
    assert analyst.assess(make_match("Ana One", "Nobody At All")) is None
    assert analyst.assess(make_match("Ana One", "Bea Two")) is None  # only one match each


def test_surname_fallback_matches_a_differently_written_name():
    rows = csv_rows(*[f"202609{i:02d},Hard,Juan Pablo Varillas,Bea Two,6-4 6-4,900,400" for i in range(1, 6)])
    st = store_with(rows)
    assert st.find("Juan Varillas") is not None
    assert st.find("Varillas") is not None


def test_disabled_analyst_returns_nothing():
    analyst = FormAnalyst(store=store_with(csv_rows()), enabled=False)
    assert analyst.assess(make_match("Ana One", "Bea Two")) is None
    assert "disabled" in analyst.status


def test_unreachable_sources_do_not_raise(monkeypatch):
    """No data source is a degraded mode, not a crash: the bot trades price alone."""
    import httpx

    def boom(*a, **kw):
        raise httpx.ConnectError("blocked")

    monkeypatch.setattr(httpx, "get", boom)
    st = FormStore(cache_dir="/tmp/kb-form-missing", cache_hours=0,
                   sources=("https://example.invalid/atp_{year}.csv",))
    assert st.load(years=[2026]) == 0
    assert all(not r["ok"] for r in st.report) and st.report
    analyst = FormAnalyst(store=st)
    assert analyst.assess(make_match("Ana One", "Bea Two")) is None
    assert "unavailable" in analyst.status


@pytest.mark.parametrize("score,sets", [("6-4 6-4", 2), ("7-6(5) 3-6 6-4", 3), ("6-2 RET", 1), ("", 0)])
def test_set_counting(score, sets):
    from kalshitrader.tennis.form import _count_sets

    assert _count_sets(score) == sets


def test_status_names_the_failure_when_no_source_resolves():
    """A bare "0/8 sources" is useless on a machine we cannot inspect; the reason has
    to travel to the dashboard with the failure."""
    from kalshitrader.tennis.form import FormAnalyst, FormStore

    store = FormStore(cache_dir="/nonexistent-cache", sources=("https://example.invalid/{year}.csv",))
    analyst = FormAnalyst(store=store)
    analyst.ensure_loaded()
    assert not store.players
    assert "sources reachable" in analyst.status
    assert "network failed" in analyst.status or "HTTP" in analyst.status


def test_no_configured_sources_is_silent_not_a_failure():
    """Shipping with no source must not look like twelve broken downloads: with
    nothing configured there is nothing to fetch, nothing to warn about, and the
    status says so rather than reporting sources that were never tried."""
    from kalshitrader.tennis.form import FormAnalyst, FormStore

    store = FormStore(cache_dir="/tmp/kb-form-none", sources=())
    assert store.load() == 0
    assert store.report == []
    analyst = FormAnalyst(store=store)
    assert "no FORM_SOURCES" in analyst.status


def test_default_sources_are_empty():
    """The URLs this once shipped pointed at a repository that does not exist. No
    default may be reintroduced without a source someone has actually fetched."""
    from kalshitrader.tennis.form import DEFAULT_SOURCES

    assert DEFAULT_SOURCES == ()
