import pytest

from kalshitrader.analysis.estimators import (
    CompositeEstimator,
    Estimate,
    ManualEstimator,
    MicrostructureEstimator,
    build_estimator,
)
from kalshitrader.kalshi.models import Market, Orderbook
from tests.conftest import make_market


def test_manual_roundtrip(tmp_path):
    me = ManualEstimator(tmp_path / "e.json")
    m = Market.from_api(make_market())
    assert me.estimate(m, None) is None
    me.set("kxtest-1", 0.62, 0.9, "polls")
    e = me.estimate(m, None)
    assert e.p_yes == 0.62 and e.confidence == 0.9 and e.source == "manual" and "polls" in e.rationale
    assert me.remove("KXTEST-1") and not me.remove("KXTEST-1")


def test_manual_tolerates_corrupt_file(tmp_path):
    p = tmp_path / "e.json"
    p.write_text("{not json")
    assert ManualEstimator(p).all() == {}


def test_microstructure_shrinks_with_spread():
    ms = MicrostructureEstimator()
    tight = ms.estimate(Market.from_api(make_market(yes_bid=69, yes_ask=71)), None)
    wide = ms.estimate(Market.from_api(make_market(yes_bid=60, yes_ask=80)), None)
    assert tight.p_yes == pytest.approx(0.70 * 0.9 + 0.5 * 0.1)
    assert wide.p_yes == pytest.approx(0.5)
    assert wide.confidence == 0.0
    assert tight.confidence <= ms.max_confidence


def test_microstructure_uses_imbalance():
    ms = MicrostructureEstimator()
    m = Market.from_api(make_market(yes_bid=49, yes_ask=51))
    heavy_yes = Orderbook.from_api(m.ticker, {"orderbook": {"yes": [[49, 900]], "no": [[49, 100]]}})
    heavy_no = Orderbook.from_api(m.ticker, {"orderbook": {"yes": [[49, 100]], "no": [[49, 900]]}})
    assert ms.estimate(m, heavy_yes).p_yes > ms.estimate(m, heavy_no).p_yes


def test_composite_blends_and_damps_disagreement():
    class Fixed:
        def __init__(self, name, p, c):
            self.name, self.p, self.c = name, p, c

        def estimate(self, market, book):
            return Estimate(self.p, self.c, self.name)

    m = Market.from_api(make_market())
    agree = CompositeEstimator([Fixed("a", 0.60, 0.8), Fixed("b", 0.62, 0.4)]).estimate(m, None)
    assert agree.p_yes == pytest.approx((0.60 * 0.8 + 0.62 * 0.4) / 1.2)
    assert agree.confidence == pytest.approx(0.8)
    disagree = CompositeEstimator([Fixed("a", 0.80, 0.8), Fixed("b", 0.30, 0.8)]).estimate(m, None)
    assert disagree.confidence < 0.8
    assert "a+b" == disagree.source


def test_composite_skips_broken_members():
    class Broken:
        name = "broken"

        def estimate(self, market, book):
            raise RuntimeError("nope")

    m = Market.from_api(make_market())
    assert CompositeEstimator([Broken()]).estimate(m, None) is None
    e = CompositeEstimator([Broken(), MicrostructureEstimator()]).estimate(m, None)
    assert e is not None and e.source == "microstructure"


def test_build_estimator_ignores_unknown(tmp_path):
    comp = build_estimator(["manual", "bogus"], manual_path=str(tmp_path / "e.json"))
    assert [e.name for e in comp.estimators] == ["manual"]
    assert [e.name for e in build_estimator([], manual_path="x").estimators] == ["microstructure"]
