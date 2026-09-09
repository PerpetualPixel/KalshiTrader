"""Player form from an open tennis-results dataset - no API key, no paid service.

There is no default source. An earlier version shipped URLs for a GitHub
repository of ATP/WTA match CSVs that turned out not to exist: every file 404ed,
the repository 404ed, and the account has no such repository. Rather than guess
at a replacement, this now ships with no sources and stays switched off until
you point it at one, so nothing fails on startup for a dataset that may not be
there.

To switch it on, set both in `.env`:

    FORM_ENABLED=true
    FORM_SOURCES=https://example.com/atp_{year}.csv,https://example.com/wta_{year}.csv

Each URL is a template with `{year}`, fetched for the last three seasons and
cached. The CSV needs the columns this parser reads - `winner_name`,
`loser_name`, `tourney_date`, `surface`, `score`, `winner_rank_points`,
`loser_rank_points` - which is the shape the well-known public tennis datasets
use. `kalshitrader form` reports which sources resolved and how many rows parsed.

From those rows the bot derives recent win rate, surface record, head-to-head,
ranking points and a deciding-set record, and turns them into the same
`MatchAssessment` the Claude analyst produces, so the strategy does not care
where the numbers came from.

Two honest limits:
  * the data is historical - it is background form, not live news about who just
    rolled an ankle;
  * a player with no matches in the files simply has no assessment, and the bot
    falls back to trading the price action alone.
"""
from __future__ import annotations

import csv
import io
import logging
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import httpx

from kalshitrader.markets.contest import Contest
from kalshitrader.tennis.analyst import MatchAssessment

log = logging.getLogger(__name__)

# No default: the URLs that used to live here pointed at a repository that does
# not exist. Set FORM_SOURCES in .env to enable this, as templates containing
# {year}.
DEFAULT_SOURCES: tuple[str, ...] = ()


def normalise(name: str) -> str:
    """Fold a player name to a comparable key: no accents, no punctuation, lowercase."""
    text = unicodedata.normalize("NFKD", name or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = "".join(c if c.isalnum() or c.isspace() else " " for c in text)
    return " ".join(text.lower().split())


def surname(name: str) -> str:
    parts = normalise(name).split()
    return parts[-1] if parts else ""


@dataclass
class PlayerRecord:
    name: str
    matches: list[dict] = field(default_factory=list)  # newest first

    @property
    def played(self) -> int:
        return len(self.matches)

    def recent(self, n: int = 12) -> list[dict]:
        return self.matches[:n]

    def win_rate(self, n: int = 12) -> float | None:
        rows = self.recent(n)
        return (sum(1 for r in rows if r["won"]) / len(rows)) if rows else None

    def surface_win_rate(self, surface: str, n: int = 20) -> float | None:
        rows = [r for r in self.matches[:n] if r["surface"].lower() == (surface or "").lower()]
        return (sum(1 for r in rows if r["won"]) / len(rows)) if rows else None

    def decider_win_rate(self, n: int = 25) -> float | None:
        """Record in matches that went to a final set - the closest proxy for whether
        a player wins from behind, which is exactly what a mid-match dip is."""
        rows = [r for r in self.matches[:n] if r["sets"] >= 3]
        return (sum(1 for r in rows if r["won"]) / len(rows)) if rows else None

    def retired_recently(self, n: int = 5) -> bool:
        return any(r["retirement"] for r in self.recent(n))

    def rank_points(self) -> int | None:
        for r in self.matches:
            if r["rank_points"]:
                return r["rank_points"]
        return None

    def head_to_head(self, opponent: str) -> tuple[int, int]:
        key = normalise(opponent)
        wins = sum(1 for r in self.matches if r["won"] and normalise(r["opponent"]) == key)
        losses = sum(1 for r in self.matches if not r["won"] and normalise(r["opponent"]) == key)
        return wins, losses


def _count_sets(score: str) -> int:
    if not score:
        return 0
    return sum(1 for part in score.split() if "-" in part)


class FormStore:
    """Downloads the season's match files and indexes them by player."""

    def __init__(self, cache_dir: str | Path = "data/form", sources: tuple[str, ...] = DEFAULT_SOURCES,
                 cache_hours: float = 12.0, timeout: float = 30.0):
        self.cache_dir = Path(cache_dir)
        self.sources = sources
        self.cache_hours = cache_hours
        self.timeout = timeout
        self.players: dict[str, PlayerRecord] = {}
        self.by_surname: dict[str, list[str]] = {}
        self.loaded_at: float | None = None
        self.report: list[dict] = []  # one row per source, for `kalshitrader form`

    # ------------------------------------------------------------- fetching
    def _cache_path(self, url: str) -> Path:
        return self.cache_dir / url.rsplit("/", 1)[-1]

    def _fetch(self, url: str) -> tuple[str | None, str]:
        """Return (csv text, note). Uses the cache when it is fresh or the network fails."""
        path = self._cache_path(url)
        if path.exists():
            age_h = (time.time() - path.stat().st_mtime) / 3600
            if age_h < self.cache_hours:
                return path.read_text(encoding="utf-8", errors="replace"), f"cached {age_h:.1f}h ago"
        try:
            resp = httpx.get(url, timeout=self.timeout, follow_redirects=True)
        except Exception as exc:
            if path.exists():
                return path.read_text(encoding="utf-8", errors="replace"), f"network failed ({type(exc).__name__}), using stale cache"
            return None, f"network failed: {type(exc).__name__}: {str(exc)[:80]}"
        if resp.status_code != 200:
            if path.exists():
                return path.read_text(encoding="utf-8", errors="replace"), f"HTTP {resp.status_code}, using cache"
            return None, f"HTTP {resp.status_code}"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(resp.text, encoding="utf-8")
        return resp.text, f"downloaded {len(resp.text) // 1024}KB"

    # -------------------------------------------------------------- loading
    def load(self, years: list[int] | None = None, force: bool = False) -> int:
        """Index the given seasons (default: this year and last). Returns match rows read."""
        if self.loaded_at and not force and (time.time() - self.loaded_at) < self.cache_hours * 3600:
            return sum(p.played for p in self.players.values())
        now = datetime.now(timezone.utc)
        # A season file appears only once that season starts, and the publisher can be
        # a few days behind, so reach back far enough that an empty current year still
        # leaves a usable prior season to work from.
        years = years or [now.year, now.year - 1, now.year - 2]
        self.players, self.by_surname, self.report = {}, {}, []
        if not self.sources:
            self.loaded_at = time.time()
            return 0
        rows = 0
        for year in years:
            for template in self.sources:
                url = template.format(year=year)
                text, note = self._fetch(url)
                if text is None:
                    self.report.append({"url": url, "ok": False, "note": note, "rows": 0})
                    log.warning("form source unavailable: %s (%s)", url.rsplit("/", 1)[-1], note)
                    continue
                n = self._index(text)
                rows += n
                self.report.append({"url": url, "ok": True, "note": note, "rows": n})
        for record in self.players.values():
            record.matches.sort(key=lambda r: r["date"], reverse=True)
        self.loaded_at = time.time()
        ok = sum(1 for r in self.report if r["ok"])
        log.info("player form: %d matches for %d players from %d/%d sources",
                 rows, len(self.players), ok, len(self.report))
        if not ok and self.report:
            # A silent "0/8" tells you nothing about whether it was DNS, a proxy or a
            # renamed file, and this runs on a machine we cannot inspect.
            log.warning("no player form data: every source failed. First failure: %s. "
                        "Run `kalshitrader form` for the full list.", self.report[0]["note"])
        return rows

    def _index(self, text: str) -> int:
        reader = csv.DictReader(io.StringIO(text))
        count = 0
        for row in reader:
            winner, loser = (row.get("winner_name") or "").strip(), (row.get("loser_name") or "").strip()
            if not winner or not loser:
                continue
            date = (row.get("tourney_date") or "").strip()
            surface = (row.get("surface") or "").strip()
            score = (row.get("score") or "").strip()
            sets = _count_sets(score)
            retired = "RET" in score.upper() or "W/O" in score.upper()
            for name, opponent, won, rank_key in ((winner, loser, True, "winner_rank_points"),
                                                  (loser, winner, False, "loser_rank_points")):
                try:
                    points = int(float(row.get(rank_key) or 0)) or None
                except ValueError:
                    points = None
                self._add(name, {"date": date, "opponent": opponent, "won": won, "surface": surface,
                                 "sets": sets, "retirement": retired and not won, "rank_points": points})
            count += 1
        return count

    def _add(self, name: str, entry: dict) -> None:
        key = normalise(name)
        record = self.players.get(key)
        if record is None:
            record = self.players[key] = PlayerRecord(name=name)
            self.by_surname.setdefault(surname(name), []).append(key)
        record.matches.append(entry)

    # -------------------------------------------------------------- lookup
    def find(self, name: str) -> PlayerRecord | None:
        key = normalise(name)
        hit = self.players.get(key)
        if hit:
            return hit
        # Kalshi and the data files disagree on initials and middle names often
        # enough that a surname match, when unambiguous, is worth taking.
        candidates = self.by_surname.get(surname(name), [])
        if len(candidates) == 1:
            return self.players[candidates[0]]
        first = key.split()[0] if key.split() else ""
        narrowed = [c for c in candidates if c.split() and c.split()[0][:1] == first[:1]]
        return self.players[narrowed[0]] if len(narrowed) == 1 else None


def _blend(base: float, other: float | None, weight: float) -> float:
    return base if other is None else base + weight * (other - 0.5) * 2


class FormAnalyst:
    """Turns open match data into the same assessment shape the Claude analyst returns.

    Cheap and synchronous: after the first load it is a dictionary lookup, so unlike
    the Claude path it can run on the trading loop without stalling it.
    """

    name = "form"

    def __init__(self, store: FormStore | None = None, enabled: bool = True, min_matches: int = 3):
        self.store = store or FormStore()
        self.enabled = enabled
        self.min_matches = min_matches

    @property
    def status(self) -> str:
        """Derived from what the store actually holds, not from a separate flag that
        can disagree with it."""
        if not self.enabled:
            return "player form disabled"
        if not self.store.sources:
            return "player form off (no FORM_SOURCES set)"
        if self.store.loaded_at is None:
            return "player form not loaded yet"
        ok = sum(1 for r in self.store.report if r["ok"])
        if not self.store.players:
            note = next((r["note"] for r in self.store.report if not r["ok"]), "")
            detail = f": {note}" if note else ""
            return f"player form unavailable (0/{len(self.store.report)} sources reachable{detail})"
        return f"player form: {len(self.store.players)} players from {ok}/{len(self.store.report)} sources"

    def ensure_loaded(self) -> None:
        if not self.enabled or self.store.loaded_at:
            return
        try:
            self.store.load()
        except Exception as exc:
            self.store.loaded_at = time.time()  # do not retry a broken source every scan
            log.warning("could not load player form data: %s", str(exc)[:200])

    def assess(self, match: Contest) -> MatchAssessment | None:
        if not self.enabled:
            return None
        self.ensure_loaded()
        a_rec, b_rec = self.store.find(match.player_a), self.store.find(match.player_b)
        if a_rec is None or b_rec is None:
            return None
        if min(a_rec.played, b_rec.played) < self.min_matches:
            return None
        surface = self._surface(match)
        p_a = self._probability(a_rec, b_rec, surface)
        facts_a = self._facts(a_rec, b_rec.name, surface)
        facts_b = self._facts(b_rec, a_rec.name, surface)
        # Confidence grows with how many matches back each player, and is capped:
        # historical form is a prior, never a read on the match in front of us.
        depth = min(a_rec.played, b_rec.played)
        confidence = min(0.6, 0.2 + 0.04 * depth)
        h2h_a, h2h_b = a_rec.head_to_head(b_rec.name)
        view = (f"{a_rec.name} {self._pct(a_rec.win_rate())} vs {b_rec.name} {self._pct(b_rec.win_rate())} "
                f"in recent matches; head-to-head {h2h_a}-{h2h_b}. Historical form only.")
        return MatchAssessment(
            player_a=match.player_a, player_b=match.player_b, tournament=match.tournament, surface=surface,
            p_a_wins=p_a,
            a=self._player(a_rec, match.player_a, surface, facts_a),
            b=self._player(b_rec, match.player_b, surface, facts_b),
            confidence=confidence, trade_view=view,
            sources=[r["url"] for r in self.store.report if r["ok"]][:4],
        )

    # ------------------------------------------------------------ internals
    @staticmethod
    def _pct(value: float | None) -> str:
        return "n/a" if value is None else f"{value * 100:.0f}%"

    @staticmethod
    def _surface(match: Contest) -> str:
        blob = f"{match.tournament} {match.title}".lower()
        for needle, surface in (("clay", "Clay"), ("grass", "Grass"), ("hard", "Hard")):
            if needle in blob:
                return surface
        return ""

    def _probability(self, a: PlayerRecord, b: PlayerRecord, surface: str) -> float:
        pa, pb = a.rank_points(), b.rank_points()
        if pa and pb:
            base = pa / (pa + pb)
            p = 0.5 + 0.7 * (base - 0.5)  # ranking points overstate the gap; shrink it
        else:
            p = 0.5
        wa, wb = a.win_rate(), b.win_rate()
        if wa is not None and wb is not None:
            p += 0.18 * (wa - wb)
        sa, sb = a.surface_win_rate(surface), b.surface_win_rate(surface)
        if surface and sa is not None and sb is not None:
            p += 0.08 * (sa - sb)
        h2h_a, h2h_b = a.head_to_head(b.name)
        if h2h_a + h2h_b:
            p += 0.06 * (h2h_a - h2h_b) / (h2h_a + h2h_b)
        return max(0.12, min(0.88, p))

    def _player(self, rec: PlayerRecord, display_name: str, surface: str, facts: list[str]) -> dict:
        form = rec.win_rate()
        decider = rec.decider_win_rate()
        surf = rec.surface_win_rate(surface) if surface else None
        return {
            "name": display_name,
            "form_score": 10 * (form if form is not None else 0.5),
            # Winning deciding sets is the closest thing in the data to recovering
            # from a mid-match hole, which is exactly the dip this bot buys.
            "comeback_score": 10 * _blend(0.5, decider, 0.4) if decider is not None else 10 * (form or 0.5),
            "surface_fit": 10 * (surf if surf is not None else (form if form is not None else 0.5)),
            "fitness_risk": 8.0 if rec.retired_recently() else 2.0,
            "key_facts": facts,
        }

    def _facts(self, rec: PlayerRecord, opponent: str, surface: str) -> list[str]:
        facts = []
        recent = rec.recent(10)
        if recent:
            facts.append(f"{sum(1 for r in recent if r['won'])}-{sum(1 for r in recent if not r['won'])} in last {len(recent)}")
        if surface and (sr := rec.surface_win_rate(surface)) is not None:
            facts.append(f"{self._pct(sr)} on {surface.lower()}")
        if (dr := rec.decider_win_rate()) is not None:
            facts.append(f"{self._pct(dr)} in deciding sets")
        h2h_w, h2h_l = rec.head_to_head(opponent)
        if h2h_w + h2h_l:
            facts.append(f"h2h {h2h_w}-{h2h_l}")
        if rec.retired_recently():
            facts.append("retired from a recent match")
        if (pts := rec.rank_points()):
            facts.append(f"{pts} ranking points")
        return facts[:6]
