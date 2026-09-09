"""Screenshot path: read a Kalshi tennis screen with Claude, research the players, score the trade."""
from __future__ import annotations

from kalshitrader.analysis.ev import net_expected_value_cents
from kalshitrader.analysis.vision import ClaudeAnalyst
from kalshitrader.config import Settings
from kalshitrader.markets.contest import Contest, Leg, parse_sides
from kalshitrader.tennis.analyst import TennisAnalyst
from kalshitrader.tracking.db import Store


def analyze_screenshot_bytes(path: str, settings: Settings, store: Store, context: str = "") -> dict:
    vision = ClaudeAnalyst(model=settings.anthropic_model, effort=settings.claude_effort, api_key=settings.anthropic_api_key)
    reading = vision.analyze_screenshot(path, extra_context=context or "This is a tennis match-winner market.")
    if reading is None:
        return {"error": "the analyst could not read the screenshot"}
    market = reading.to_market()
    players = parse_sides(reading.title) or parse_sides(reading.settlement_criteria)
    result: dict = {"reading": reading.model_dump(), "assessment": None, "verdicts": []}
    if not players:
        result["error"] = "could not identify two players in the title"
        return result
    a, b = players
    legs = {a: Leg(a, market.ticker, "yes", market), b: Leg(b, market.ticker, "no", market)}
    match = Contest(key=f"SHOT:{a} vs {b}", a=a, b=b, legs=legs, markets=[market])
    analyst = TennisAnalyst(store, model=settings.anthropic_model, effort=settings.claude_effort,
                            ttl_minutes=settings.assessment_ttl_minutes, max_searches=settings.research_max_searches,
                            enabled=settings.research_enabled, api_key=settings.anthropic_api_key)
    assessment = analyst.assess(match)
    if assessment is None:
        result["error"] = f"no research available ({analyst.status})"
        return result
    result["assessment"] = assessment.model_dump()
    for player, leg in legs.items():
        p_pre, pa = assessment.for_player(player)
        ask = leg.ask
        ev_net = net_expected_value_cents(p_pre, ask, settings.fee_rate) if ask else None
        gates = []
        if pa.form_score < settings.min_form_score:
            gates.append(f"form {pa.form_score:.1f} below {settings.min_form_score:.1f}")
        if pa.comeback_score < settings.min_comeback_score:
            gates.append(f"comeback {pa.comeback_score:.1f} below {settings.min_comeback_score:.1f}")
        if p_pre < settings.min_pre_match_p_win:
            gates.append(f"pre-match P(win) {p_pre:.2f} below {settings.min_pre_match_p_win:.2f}")
        if ev_net is not None and ev_net < settings.min_edge_cents:
            gates.append(f"edge {ev_net:+.1f}c at {ask}c below {settings.min_edge_cents:.1f}c")
        verdict = "BUYABLE on a dip" if not gates else "PASS"
        result["verdicts"].append({
            "player": player, "side": leg.side, "ask": ask, "p_pre": p_pre, "form": pa.form_score, "comeback": pa.comeback_score,
            "ev_net": ev_net, "verdict": verdict, "blockers": gates,
            "sell_target": min(99, ask + settings.take_profit_cents, int(round(p_pre * 100))) if ask and not gates else None,
            "stop": max(1, ask - settings.stop_loss_cents) if ask and not gates else None,
        })
    return result
