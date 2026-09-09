import json
from types import SimpleNamespace

from kalshitrader.analysis.estimators import ClaudeMarketEstimator
from kalshitrader.analysis.vision import ClaudeAnalyst, ScreenAnalysis
from kalshitrader.kalshi.models import Market
from tests.conftest import make_market

READING = {
    "ticker": "KXBTC-25SEP08-T60000", "title": "BTC above $60k on Sep 8?",
    "settlement_criteria": "CF Benchmarks BRR at 5pm ET >= 60000", "close_time": "2026-09-08T21:00:00Z",
    "yes_bid": 41, "yes_ask": 43, "no_bid": 57, "no_ask": 59, "last_price": 42, "volume": 12000,
    "depth_at_ask": 350, "momentum": "up, three green ticks", "p_true": 0.55, "confidence": 0.6,
    "rationale": "spot is 2% above strike with 30h left",
}


class FakeMessages:
    def __init__(self, text=None, stop_reason="end_turn"):
        self.text = json.dumps(READING) if text is None else text
        self.stop_reason = stop_reason
        self.calls = []

    def create(self, **kw):
        self.calls.append(kw)
        return SimpleNamespace(stop_reason=self.stop_reason, content=[SimpleNamespace(type="text", text=self.text)])


def fake_client(**kw):
    return SimpleNamespace(messages=FakeMessages(**kw))


def test_screenshot_analysis_parses_and_scores(tmp_path):
    img = tmp_path / "shot.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\nfake")
    c = fake_client()
    analyst = ClaudeAnalyst(client=c, model="claude-opus-5")
    reading = analyst.analyze_screenshot(img, extra_context="BTC spot 61.2k")
    assert isinstance(reading, ScreenAnalysis) and reading.p_true == 0.55
    call = c.messages.calls[0]
    assert call["model"] == "claude-opus-5"
    assert call["thinking"] == {"type": "adaptive"}
    assert call["output_config"]["format"]["type"] == "json_schema"
    assert call["output_config"]["format"]["schema"]["additionalProperties"] is False
    assert call["messages"][0]["content"][0]["type"] == "image"
    assert "BTC spot" in call["messages"][0]["content"][1]["text"]
    m = reading.to_market()
    assert m.yes_ask == 43 and m.no_ask == 59 and m.close_time is not None


def test_refusal_returns_none(tmp_path):
    img = tmp_path / "shot.png"
    img.write_bytes(b"x")
    assert ClaudeAnalyst(client=fake_client(stop_reason="refusal")).analyze_screenshot(img) is None


def test_market_estimator_wraps_analyst():
    est = ClaudeMarketEstimator(client=fake_client())
    e = est.estimate(Market.from_api(make_market()), None)
    assert e.p_yes == 0.55 and e.confidence == 0.6 and e.source == "claude"


def test_market_estimator_swallows_bad_json():
    est = ClaudeMarketEstimator(client=fake_client(text="not json"))
    assert est.estimate(Market.from_api(make_market()), None) is None


def test_out_of_range_values_are_clamped_not_rejected():
    """The API rejects numeric bounds in the schema, so they are enforced after parsing.

    Clamping matters: rejecting would throw away an otherwise good reading because one
    number came back as 1.02.
    """
    assert ScreenAnalysis.model_validate({**READING, "p_true": 1.7}).p_true == 1.0
    assert ScreenAnalysis.model_validate({**READING, "confidence": -0.3}).confidence == 0.0


def test_schema_sent_to_the_api_carries_no_numeric_bounds():
    """A schema with minimum/maximum is rejected: 'For number type, properties
    maximum, minimum are not supported'."""
    import json

    from kalshitrader.tennis.analyst import api_json_schema

    blob = json.dumps(api_json_schema(ScreenAnalysis))
    for keyword in ("minimum", "maximum", "exclusiveMinimum", "maxItems", "maxLength"):
        assert keyword not in blob, keyword
    # the ranges survive as guidance in the descriptions
    assert "between 0 and 1" in blob
