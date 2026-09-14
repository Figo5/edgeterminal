"""Pydantic contracts. Every provider response and LLM response is validated
against one of these before it reaches the rest of the app."""
from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator

Sport = Literal["NFL", "CFB", "NBA", "PGA"]
MarketType = Literal["moneyline", "spread", "total", "team_total", "player_prop",
                     "outright", "top5", "top10", "top20", "make_cut", "matchup", "parlay"]
State = Literal["OFFICIAL PICK", "LEAN", "WATCHLIST", "WAIT FOR PRICE", "PASS",
                "NO EDGE", "STALE", "NO LONGER PLAYABLE"]


class Quote(BaseModel):
    book: str
    price: float
    line: Optional[float] = None
    captured_at: Optional[float] = None

    @field_validator("price")
    @classmethod
    def _sane_price(cls, v: float) -> float:
        if -100 < v < 100:
            raise ValueError(f"implausible american price {v}")
        return v


class MarketQuotes(BaseModel):
    event_id: str
    market: MarketType
    selection: str
    quotes: List[Quote] = Field(default_factory=list)


class Event(BaseModel):
    id: str
    sport: Sport
    name: str
    home_team: Optional[str] = None
    away_team: Optional[str] = None
    start_time: Optional[str] = None
    venue: Optional[str] = None
    status: str = "scheduled"
    meta: Dict[str, Any] = Field(default_factory=dict)


class TeamStats(BaseModel):
    team: str
    games: int = 0
    points_for: float = 0.0
    points_against: float = 0.0
    pace: Optional[float] = None
    off_rating: Optional[float] = None
    def_rating: Optional[float] = None
    extra: Dict[str, Any] = Field(default_factory=dict)
    source: str = "unknown"
    retrieved_at: Optional[float] = None


class InjuryItem(BaseModel):
    team: Optional[str] = None
    player: str
    position: Optional[str] = None
    status: str
    detail: Optional[str] = None
    source: str = "unknown"
    retrieved_at: Optional[float] = None


class NewsItem(BaseModel):
    title: str
    url: Optional[str] = None
    publisher: Optional[str] = None
    published: Optional[str] = None
    summary: Optional[str] = None
    trust: str = "reported"      # reported | official | community | unverified


class WeatherReport(BaseModel):
    temp_f: Optional[float] = None
    wind_mph: Optional[float] = None
    wind_dir: Optional[str] = None
    precip_chance: Optional[float] = None
    conditions: Optional[str] = None
    indoor: bool = False
    source: str = "unknown"
    retrieved_at: Optional[float] = None


class AgentSignal(BaseModel):
    """Structured intermediate output. Agents never pass prose blobs around."""
    agent: str
    market: Optional[str] = None
    selection: Optional[str] = None
    direction: float = 0.0        # -1 .. +1 relative to the evaluated side
    strength: Literal["strong", "moderate", "slight", "neutral"] = "neutral"
    note: str = ""
    inputs: Dict[str, Any] = Field(default_factory=dict)


class SourceRef(BaseModel):
    kind: str
    title: str
    url: Optional[str] = None
    publisher: Optional[str] = None
    retrieved_at: Optional[float] = None
    trust: str = "unverified"


class Recommendation(BaseModel):
    id: str
    event_id: str
    event_name: str
    sport: Sport
    start_time: Optional[str] = None
    market: MarketType
    market_label: str
    selection: str
    side: Optional[str] = None
    line: Optional[float] = None
    book: str
    price: float
    all_quotes: List[Quote] = Field(default_factory=list)
    model_prob: float
    implied_prob: float
    fair_price: float
    edge_pct: float
    ev_per_unit: float
    confidence: float
    confidence_bucket: str
    confidence_components: Dict[str, float] = Field(default_factory=dict)
    units: float
    state: State
    data_quality: str
    breakeven_price: Optional[float] = None
    trigger_note: Optional[str] = None
    line_movement: Dict[str, Any] = Field(default_factory=dict)
    signals: List[AgentSignal] = Field(default_factory=list)
    reasoning: List[str] = Field(default_factory=list)
    risks: List[str] = Field(default_factory=list)
    sources: List[SourceRef] = Field(default_factory=list)
    model_version: str = ""
    confidence_version: str = ""
    created_at: float = 0.0
    stale: bool = False
    is_demo: bool = False


class ParlayLeg(BaseModel):
    recommendation_id: Optional[str] = None
    event_id: str
    event_name: str
    sport: Sport
    market: MarketType
    selection: str
    line: Optional[float] = None
    book: str
    price: float
    model_prob: float
    confidence: float
    why: str = ""


class Parlay(BaseModel):
    legs: List[ParlayLeg]
    combined_price: int
    independent_prob: float
    correlation_adjusted_prob: float
    implied_prob: float
    edge_pct: float
    ev_per_unit: float
    confidence: float
    units: float
    correlation_notes: List[str] = Field(default_factory=list)
    rationale: List[str] = Field(default_factory=list)
    risks: List[str] = Field(default_factory=list)
    state: State = "LEAN"


class ProviderStatus(BaseModel):
    name: str
    kind: str
    ok: bool
    detail: str = ""
    is_demo: bool = False
    last_success: Optional[float] = None
