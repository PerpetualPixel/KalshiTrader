"""Tennis research analyst: player form, matchup and swing suitability, via Claude.

Two calls per match, cached in the Store for `assessment_ttl_minutes`:
  1. research  - Claude with the web-search server tool gathers recent form,
                 surface record, head-to-head, fitness/injury news and the
                 tournament context, then writes a short expert brief.
  2. structure - a second call turns that brief into a strict `MatchAssessment`.

Without the `anthropic` package or an API key, `TennisAnalyst.assess` returns
None and the strategy PASSes unless "trade without research" is enabled.
"""
from __future__ import annotations

import json
import logging
import queue
import threading
from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict, Field, field_validator

from kalshitrader.markets.contest import Contest
from kalshitrader.tracking.db import Store

log = logging.getLogger(__name__)

# Keywords the structured-output API rejects. Pydantic emits them from Field(ge=...),
# and a request carrying them fails with
#   "For 'number' type, properties maximum, minimum are not supported".
_UNSUPPORTED_SCHEMA_KEYS = (
    "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf",
    "minItems", "maxItems", "minLength", "maxLength", "pattern", "format",
)


def api_json_schema(model: type[BaseModel]) -> dict:
    """A model's JSON schema with constraint keywords the API does not accept removed.

    The bounds stay in each field's description so the model still knows the ranges,
    and validators clamp whatever comes back.
    """

    def scrub(node):
        if isinstance(node, dict):
            return {k: scrub(v) for k, v in node.items() if k not in _UNSUPPORTED_SCHEMA_KEYS}
        if isinstance(node, list):
            return [scrub(v) for v in node]
        return node

    return scrub(model.model_json_schema())

RESEARCH_SYSTEM = """You are a veteran tennis analyst and a disciplined prediction-market trader.
You are briefing a trading desk on one professional match that may already be in progress.
Use web search to find CURRENT information: last 5-10 results for each player, surface-specific
form this season, head-to-head, ranking trajectory, injury or fatigue signals (retirements, long
matches in previous rounds, doubles commitments), and any pre-match/live news. Prefer reputable
sources (ATP/WTA sites, Tennis Abstract, Flashscore, ESPN, major press).
Then reason as a trader: which player, if their price drops during the match after losing a set or
a break, is genuinely likely to swing it back, and which is not. Consider serve strength (holding
under pressure), return game quality, fitness for long matches, mental toughness in deciders, and
style matchup. Be concrete and cite the facts you found. Say clearly when information is thin."""

STRUCTURE_SYSTEM = """Convert the analyst brief into the JSON schema exactly. Probabilities are for the
match winner BEFORE the match started (pre-match), independent of any market price. Scores are
0-10. Confidence in [0,1] reflects how much verified, recent information supports the numbers.
Never invent facts not in the brief; when the brief says information is thin, lower confidence."""


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


class PlayerAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    form_score: float = Field(..., description="Recent form and match sharpness, 0-10")
    comeback_score: float = Field(..., description="How plausible a swing back is after falling behind, 0-10: serve, fitness, mentality, style vs this opponent")
    surface_fit: float = Field(..., description="Suitability of this surface for the player, 0-10")
    fitness_risk: float = Field(..., description="0 = fresh and healthy, 10 = injured or exhausted")
    key_facts: list[str] = Field(default_factory=list)

    @field_validator("form_score", "comeback_score", "surface_fit", "fitness_risk", mode="after")
    @classmethod
    def _bound(cls, v: float) -> float:
        # Bounds are described in the prompt rather than the schema (the API rejects
        # numeric minimum/maximum), so enforce them here instead of failing the parse.
        return _clamp(v, 0.0, 10.0)


class MatchAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    player_a: str
    player_b: str
    tournament: str = ""
    surface: str = ""
    p_a_wins: float = Field(..., description="Pre-match probability that player_a wins, between 0 and 1")
    a: PlayerAssessment
    b: PlayerAssessment
    confidence: float = Field(..., description="How much verified information supports these numbers, between 0 and 1")
    trade_view: str = Field(..., description="One or two sentences: which player is worth buying on a dip and why, or why neither")
    sources: list[str] = Field(default_factory=list)

    @field_validator("p_a_wins", "confidence", mode="after")
    @classmethod
    def _unit(cls, v: float) -> float:
        return _clamp(v, 0.0, 1.0)

    def for_player(self, name: str) -> tuple[float, PlayerAssessment]:
        if name == self.player_a or name.lower() == self.a.name.lower():
            return self.p_a_wins, self.a
        return 1.0 - self.p_a_wins, self.b


class TennisAnalyst:
    def __init__(self, store: Store, model: str = "claude-opus-5", effort: str = "high", ttl_minutes: int = 180,
                 max_searches: int = 6, client=None, enabled: bool = True, api_key: str | None = None):
        self.store = store
        self.model = model
        self.effort = effort
        self.ttl = ttl_minutes
        self.max_searches = max_searches
        self.enabled = enabled
        self.api_key = api_key
        self._client = client
        self._client_error: str | None = None
        # Set when a failure is clearly configuration rather than bad luck (a missing
        # or rejected key). Retrying that every scan burns cycles and floods the log.
        self._fatal: str | None = None
        # Research takes minutes per match (web search + reasoning). It must never run
        # on the trading loop: a blocked cycle is a cycle where stop-losses are not
        # checked. A worker thread does the work; the loop only ever reads the cache.
        self._queue: queue.Queue = queue.Queue()
        self._queued: set[str] = set()
        self._lock = threading.Lock()
        self._worker: threading.Thread | None = None
        self.max_queue = 12

    # ---------------------------------------------------------------- client
    @property
    def client(self):
        if self._client is None and self._client_error is None:
            try:
                import anthropic

                # The key must be passed explicitly: reading a .env file does not
                # populate os.environ, which is the only place the SDK looks.
                self._client = anthropic.Anthropic(api_key=self.api_key) if self.api_key else anthropic.Anthropic()
            except ImportError:
                self._client_error = "anthropic package not installed (pip install 'kalshitrader[ai]')"
            except Exception as exc:  # missing key etc.
                self._client_error = f"{type(exc).__name__}: {exc}"
        return self._client

    @property
    def available(self) -> bool:
        return self.enabled and self._fatal is None and self.client is not None

    def reset_errors(self) -> None:
        """Forget a fatal configuration error so the next scan retries (keys changed)."""
        self._fatal = None
        self._client = None
        self._client_error = None

    @property
    def status(self) -> str:
        if not self.enabled:
            return "research disabled"
        if self._fatal:
            return self._fatal
        if self.client is None:
            return self._client_error or "no client"
        return "ready"

    # ------------------------------------------------------------------ api
    def cached(self, match: Contest) -> MatchAssessment | None:
        payload = self.store.get_assessment(match.key)
        return MatchAssessment.model_validate(payload) if payload else None

    # ------------------------------------------------------------ background
    @property
    def pending(self) -> int:
        with self._lock:
            return len(self._queued)

    def request(self, match: Contest) -> MatchAssessment | None:
        """Return the cached assessment, queueing background research when there is none.

        Never blocks: the trading loop must keep checking exits while Claude works.
        """
        hit = self.cached(match)
        if hit or not self.available:
            return hit
        with self._lock:
            if match.key in self._queued or len(self._queued) >= self.max_queue:
                return None
            self._queued.add(match.key)
            self._queue.put(match)
            self._ensure_worker()
        return None

    def _ensure_worker(self) -> None:
        if self._worker is None or not self._worker.is_alive():
            self._worker = threading.Thread(target=self._run_worker, name="tennis-research", daemon=True)
            self._worker.start()

    def _run_worker(self) -> None:
        while True:
            try:
                match = self._queue.get(timeout=60)
            except queue.Empty:
                return  # idle: let the thread go, `request` starts a new one on demand
            try:
                if self.available:
                    self.assess(match)
            except Exception as exc:  # a worker must never die on one bad match
                log.warning("background research crashed for %s: %s", match.title, str(exc)[:200])
            finally:
                with self._lock:
                    self._queued.discard(match.key)
                self._queue.task_done()

    # ------------------------------------------------------------ synchronous
    def assess(self, match: Contest) -> MatchAssessment | None:
        """Research a match now. Blocks for minutes; use `request` on the trading loop."""
        hit = self.cached(match)
        if hit:
            return hit
        if not self.available:
            return None
        try:
            brief = self._research(match)
            assessment = self._structure(match, brief)
        except Exception as exc:
            text = str(exc)
            if _is_configuration_error(text):
                self._fatal = f"Anthropic key rejected or missing: {text[:120]}"
                log.error("research disabled until the Anthropic key is fixed: %s", text[:200])
            else:
                log.warning("research failed for %s: %s", match.title, text[:200])
            return None
        if assessment is None:
            return None
        self.store.put_assessment(match.key, assessment.model_dump(), self.ttl)
        log.info("ASSESSED %s: P(%s)=%.2f form %.1f/%.1f comeback %.1f/%.1f conf %.2f", match.title, assessment.player_a,
                 assessment.p_a_wins, assessment.a.form_score, assessment.b.form_score, assessment.a.comeback_score,
                 assessment.b.comeback_score, assessment.confidence)
        return assessment

    # ------------------------------------------------------------ internals
    def _research(self, match: Contest) -> str:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        prompt = (
            f"Today is {today}. Brief me on the tennis match {match.player_a} vs {match.player_b}"
            + (f" at {match.tournament}" if match.tournament else "")
            + ". Cover both players' recent form, surface form, head-to-head, fitness/injury signals, and give a pre-match "
            "win probability for each. Finish with a trader's view on which player is worth buying if their price dips mid-match."
        )
        tools = [{"type": "web_search_20260209", "name": "web_search", "max_uses": self.max_searches}]
        messages: list[dict] = [{"role": "user", "content": prompt}]
        response = None
        for _ in range(4):  # pause_turn continuations
            response = self.client.messages.create(
                model=self.model, max_tokens=8000, system=RESEARCH_SYSTEM, thinking={"type": "adaptive"},
                output_config={"effort": self.effort}, tools=tools, messages=messages,
            )
            if response.stop_reason != "pause_turn":
                break
            messages = messages + [{"role": "assistant", "content": response.content}]
        if response is None or response.stop_reason == "refusal":
            raise RuntimeError("no research response")
        text = "".join(getattr(b, "text", "") for b in response.content if getattr(b, "type", "") == "text")
        if not text.strip():
            raise RuntimeError("empty research brief")
        return text

    def _structure(self, match: Contest, brief: str) -> MatchAssessment | None:
        prompt = (
            f"player_a = {match.player_a}\nplayer_b = {match.player_b}\n\nAnalyst brief:\n{brief}\n\n"
            "Produce the MatchAssessment JSON."
        )
        response = self.client.messages.create(
            model=self.model, max_tokens=4000, system=STRUCTURE_SYSTEM, thinking={"type": "adaptive"},
            output_config={"effort": "medium", "format": {"type": "json_schema", "schema": api_json_schema(MatchAssessment)}},
            messages=[{"role": "user", "content": prompt}],
        )
        if response.stop_reason == "refusal":
            return None
        text = "".join(getattr(b, "text", "") for b in response.content if getattr(b, "type", "") == "text")
        if not text.strip():
            return None
        data = json.loads(text)
        data.setdefault("player_a", match.player_a)
        data.setdefault("player_b", match.player_b)
        return MatchAssessment.model_validate(data)


_CONFIG_ERROR_MARKERS = (
    "could not resolve authentication",
    "authentication_error",
    "invalid x-api-key",
    "invalid api key",
    "credit balance",
    "permission_error",
    "401",
    "403",
)


def _is_configuration_error(message: str) -> bool:
    """True when retrying will not help until the operator changes something."""
    low = message.lower()
    return any(marker in low for marker in _CONFIG_ERROR_MARKERS)
