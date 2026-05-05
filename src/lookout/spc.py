"""SPC fetcher: HTTP client, GeoJSON parser, and point-in-polygon evaluation.

Currently covers Day 1–3 categorical convective outlooks. Day 4–8 probabilistic and
mesoscale discussions are TODO; the abstractions here (SPCClient, Outlook,
evaluate_location) are designed to extend cleanly once those products' schemas can
be inspected against real data (D4–8 frequently shows "Predictability Too Low" with
no probability features to design against).

Hash-based dedup: SPC's server may not honor If-Modified-Since reliably, so we hash
the response body and skip parsing on unchanged bodies. Hashes are in-memory only —
on restart we re-parse on first fetch, but the diff engine produces no events when
observations match persisted state, so restart is idempotent.
"""

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Callable, Optional

import httpx
import shapely.geometry as shp
from shapely.geometry.base import BaseGeometry

from lookout.config import RiskLevel
from lookout import __version__

logger = logging.getLogger(__name__)

URLS_CATEGORICAL = {
    1: "https://www.spc.noaa.gov/products/outlook/day1otlk_cat.lyr.geojson",
    2: "https://www.spc.noaa.gov/products/outlook/day2otlk_cat.lyr.geojson",
    3: "https://www.spc.noaa.gov/products/outlook/day3otlk_cat.lyr.geojson",
}

URLS_PROBABILISTIC = {
    n: f"https://www.spc.noaa.gov/products/exper/day4-8/day{n}prob.lyr.geojson"
    for n in (4, 5, 6, 7, 8)
}

URL_MD_RSS = "https://www.spc.noaa.gov/products/spcmdrss.xml"

# SPC Day 4-8 publishes "any severe weather within 25 miles" probability percentages.
# These map to categorical equivalents per SPC convention so a single threshold concept
# (RiskLevel) works across all products. 5% (the lowest D4-8 category) has no D1-3
# categorical analog and is omitted — TSTM is "general thunderstorms" which doesn't
# apply to multi-day severe weather forecasts.
PROBABILITY_TO_RISK: dict[int, RiskLevel] = {
    15: RiskLevel.MRGL,
    30: RiskLevel.SLGT,
    45: RiskLevel.ENH,
    60: RiskLevel.MDT,
}


class FetchError(Exception):
    """Raised when SPC could not be reached or returned an unparseable response."""


@dataclass(frozen=True)
class RiskPolygon:
    risk: RiskLevel
    geometry: BaseGeometry  # Polygon or MultiPolygon
    valid_iso: datetime
    expire_iso: datetime


@dataclass(frozen=True)
class Outlook:
    """A parsed SPC outlook product, ready for per-location evaluation."""

    outlook_day: int               # 1-8
    issued_at: datetime
    valid_iso: datetime            # earliest VALID across features
    polygons: list[RiskPolygon]    # sorted ascending by severity

    @property
    def target_date(self) -> date:
        """The convective day this outlook covers.

        SPC convective days run 12Z → 12Z. A VALID time of e.g. 01Z May 5 belongs
        to the May 4 convective day (overnight portion). Subtracting 12h shifts
        any VALID time within [12Z day-N, 12Z day-N+1) onto day-N.
        """
        return (self.valid_iso - timedelta(hours=12)).date()


class SPCClient:
    """HTTP client for SPC products. Reuses a connection, dedups by body hash."""

    def __init__(self, user_agent_contact: str, timeout: float = 30.0):
        self._client = httpx.Client(
            headers={
                "User-Agent": f"Lookout/{__version__} ({user_agent_contact})",
            },
            timeout=timeout,
            transport=httpx.HTTPTransport(retries=2),
        )
        self._hashes: dict[str, str] = {}

    def fetch_categorical(self, day: int) -> Optional[Outlook]:
        """Fetch a Day-N (1-3) categorical outlook. Returns None if body is unchanged."""
        if day not in URLS_CATEGORICAL:
            raise ValueError(
                f"day {day} has no categorical URL "
                f"(categorical only available for D1–3; D4–8 are probabilistic)"
            )
        return self._fetch(URLS_CATEGORICAL[day], outlook_day=day, parser=parse_categorical_outlook)

    def fetch_probabilistic(self, day: int) -> Optional[Outlook]:
        """Fetch a Day-N (4-8) probabilistic outlook. Returns None if body is unchanged."""
        if day not in URLS_PROBABILISTIC:
            raise ValueError(
                f"day {day} has no probabilistic URL "
                f"(probabilistic only available for D4–8; D1–3 are categorical)"
            )
        return self._fetch(
            URLS_PROBABILISTIC[day], outlook_day=day, parser=parse_probabilistic_outlook
        )

    def fetch_md_rss(self) -> Optional[bytes]:
        """Fetch the MD RSS feed. Returns None if unchanged since last call."""
        return self._fetch_raw(URL_MD_RSS, dedup=True)

    def fetch_md_html(self, url: str) -> bytes:
        """Fetch a single MD HTML page. NOT hash-deduped — every call re-fetches.

        Why: dedup at this layer would skip parsing on unchanged HTML, but DB
        idempotency (MesoscaleDiscussionAlert) is what actually owns "have we
        already alerted?" Re-parsing a few KB of unchanged HTML per cycle is
        cheap, and avoids the bug where a transient dispatch failure would be
        masked by a stale hash on the next cycle.
        """
        body = self._fetch_raw(url, dedup=False)
        assert body is not None, "dedup=False guarantees a non-None return"
        return body

    def _fetch(
        self,
        url: str,
        *,
        outlook_day: int,
        parser: Callable[..., "Outlook"],
    ) -> Optional[Outlook]:
        body = self._fetch_raw(url, dedup=True)
        if body is None:
            return None
        return parser(body, outlook_day=outlook_day)

    def _fetch_raw(self, url: str, *, dedup: bool) -> Optional[bytes]:
        try:
            resp = self._client.get(url)
            resp.raise_for_status()
        except httpx.HTTPError as e:
            raise FetchError(f"failed to fetch {url}: {e}") from e

        body = resp.content
        if dedup:
            body_hash = hashlib.sha256(body).hexdigest()
            if self._hashes.get(url) == body_hash:
                return None
            self._hashes[url] = body_hash
        return body

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "SPCClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def parse_categorical_outlook(body: bytes, *, outlook_day: int) -> Outlook:
    """Parse a SPC categorical outlook GeoJSON FeatureCollection into an Outlook.

    Each feature represents one risk-level area. Features with unknown LABEL values
    (e.g. "Predictability Too Low") are skipped. The result's polygons are sorted
    ascending by severity so callers iterating for highest-risk-containing-point
    don't need to re-sort.
    """
    data = json.loads(body)
    polygons: list[RiskPolygon] = []
    earliest_valid: Optional[datetime] = None
    issued_at: Optional[datetime] = None

    for feature in data.get("features", []):
        props = feature.get("properties") or {}
        label = props.get("LABEL")
        if not label:
            continue
        try:
            risk = RiskLevel(label)
        except ValueError:
            # Unknown risk label (e.g. "Predictability Too Low" on D4-8 quiet days)
            continue

        valid_str = props.get("VALID_ISO")
        expire_str = props.get("EXPIRE_ISO")
        issue_str = props.get("ISSUE_ISO")
        if not (valid_str and expire_str and issue_str):
            continue
        valid = datetime.fromisoformat(valid_str)
        expire = datetime.fromisoformat(expire_str)
        issue = datetime.fromisoformat(issue_str)

        geometry = shp.shape(feature["geometry"])
        polygons.append(
            RiskPolygon(risk=risk, geometry=geometry, valid_iso=valid, expire_iso=expire)
        )

        if earliest_valid is None or valid < earliest_valid:
            earliest_valid = valid
        if issued_at is None:
            issued_at = issue

    if earliest_valid is None or issued_at is None:
        # No usable features (e.g., D4-8 "Predictability Too Low" only).
        # Return an empty outlook with sentinel values so callers can produce
        # ObservedRisk(risk=None) for every location.
        raise FetchError(
            f"outlook day {outlook_day} contained no parseable risk features"
        )

    polygons.sort(key=lambda p: p.risk)
    return Outlook(
        outlook_day=outlook_day,
        issued_at=issued_at,
        valid_iso=earliest_valid,
        polygons=polygons,
    )


def parse_probabilistic_outlook(body: bytes, *, outlook_day: int) -> Outlook:
    """Parse a SPC Day 4-8 probabilistic outlook GeoJSON.

    Probability percentages are mapped to categorical equivalents via
    `PROBABILITY_TO_RISK` so the rest of the system can work in a single threshold
    space across all products.

    Sentinel features ("Predictability Too Low" / "Potential Too Low") are
    skipped — they signal "no forecast confidence" — but their VALID/ISSUE
    timestamps are still used so an outlook with *only* sentinels still has
    valid timing. An outlook with no probability polygons returns successfully
    with empty `polygons`; every location will then evaluate to risk=None.
    """
    data = json.loads(body)
    polygons: list[RiskPolygon] = []
    earliest_valid: Optional[datetime] = None
    issued_at: Optional[datetime] = None
    fallback_valid: Optional[datetime] = None
    fallback_issued: Optional[datetime] = None

    for feature in data.get("features", []):
        props = feature.get("properties") or {}
        valid_str = props.get("VALID_ISO")
        expire_str = props.get("EXPIRE_ISO")
        issue_str = props.get("ISSUE_ISO")
        if not (valid_str and expire_str and issue_str):
            continue
        valid = datetime.fromisoformat(valid_str)
        expire = datetime.fromisoformat(expire_str)
        issue = datetime.fromisoformat(issue_str)

        # Capture timing even from sentinel features so empty outlooks still have
        # a valid target_date.
        if fallback_valid is None or valid < fallback_valid:
            fallback_valid = valid
        if fallback_issued is None:
            fallback_issued = issue

        prob = _extract_probability(props)
        if prob is None:
            continue

        risk = PROBABILITY_TO_RISK.get(prob)
        if risk is None:
            logger.debug(
                "ignoring %d%% probability on day %d: no RiskLevel mapping",
                prob, outlook_day,
            )
            continue

        geometry = shp.shape(feature["geometry"])
        polygons.append(
            RiskPolygon(risk=risk, geometry=geometry, valid_iso=valid, expire_iso=expire)
        )

        if earliest_valid is None or valid < earliest_valid:
            earliest_valid = valid
        if issued_at is None:
            issued_at = issue

    valid_final = earliest_valid or fallback_valid
    issue_final = issued_at or fallback_issued
    if valid_final is None or issue_final is None:
        raise FetchError(
            f"probabilistic outlook day {outlook_day} has no usable timing properties"
        )

    polygons.sort(key=lambda p: p.risk)
    return Outlook(
        outlook_day=outlook_day,
        issued_at=issue_final,
        valid_iso=valid_final,
        polygons=polygons,
    )


def _extract_probability(props: dict) -> Optional[int]:
    """Extract the integer percentage from an outlook feature.

    Tries LABEL first (handles "15", "15%", "15 %"); falls back to DN. Returns
    None for features that don't carry a probability (e.g. "Predictability Too Low",
    DN=0).
    """
    label = props.get("LABEL")
    if isinstance(label, str):
        match = re.match(r"\s*(\d+)\s*%?\s*$", label)
        if match:
            value = int(match.group(1))
            if value > 0:
                return value

    dn = props.get("DN")
    if isinstance(dn, (int, float)) and dn > 0:
        return int(dn)

    return None


def evaluate_location(outlook: Outlook, lat: float, lon: float) -> Optional[RiskLevel]:
    """Return the highest risk level whose polygon contains (lat, lon), or None.

    Uses `intersects()` so points exactly on a polygon boundary are inclusive —
    we'd rather over-alert than miss a borderline location.
    """
    point = shp.Point(lon, lat)  # GeoJSON ordering: longitude first
    highest: Optional[RiskLevel] = None
    for poly in outlook.polygons:
        if poly.geometry.intersects(point):
            if highest is None or poly.risk > highest:
                highest = poly.risk
    return highest
