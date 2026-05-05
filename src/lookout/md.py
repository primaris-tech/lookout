"""Mesoscale Discussion fetching, parsing, and orchestration.

Unlike the convective outlooks, MDs aren't published as GeoJSON. Their polygon
coordinates live inside the text product itself, in a `LAT...LON` block at the
bottom of the discussion. Discovery is via the SPC RSS feed; per-MD pages live
at `/products/md/mdNNNN.html`.

Coordinate format (NWS standard): each vertex is an 8-digit code `LLLLOOOO`.
LLLL is latitude × 100 (always 4 digits, north positive). OOOO is West longitude
× 100, with the leading "1" dropped when ≥ 100°W. Disambiguation: a raw 4-digit
value < 3000 is interpreted as west-of-100 (add 100°W); ≥ 3000 is east-of-100
(use as-is). Threshold of 3000 is safe for CONUS (which spans ~65°W to ~125°W).
"""

import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Optional

import shapely.geometry as shp
from shapely.geometry import Polygon

from lookout.spc import FetchError, SPCClient

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MDListing:
    """One <item> entry in the SPC MD RSS feed."""

    mcd_id: str       # zero-padded, e.g. "0644"
    title: str        # raw RSS <title> value
    html_url: str


@dataclass(frozen=True)
class MesoscaleDiscussion:
    """A parsed MD ready for evaluation against locations."""

    mcd_id: str
    title: str
    areas: Optional[str]        # 'Areas affected...' line value
    concerning: Optional[str]   # 'Concerning...' line value
    valid_raw: Optional[str]    # raw 'Valid 050601Z - 050800Z' line
    summary: Optional[str]      # 'SUMMARY...' content (single paragraph)
    polygon: Polygon
    html_url: str


_NO_MDS_PATTERN = re.compile(r"No MDs are in effect", re.IGNORECASE)
_MD_LINK_PATTERN = re.compile(r"md(\d{4})\.html")


def parse_rss(body: bytes) -> list[MDListing]:
    """Parse the SPC MD RSS feed. Returns [] when no MDs are in effect."""
    root = ET.fromstring(body)
    listings: list[MDListing] = []

    for item in root.iterfind(".//item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        description = (item.findtext("description") or "")
        if _NO_MDS_PATTERN.search(title) or _NO_MDS_PATTERN.search(description):
            continue

        match = _MD_LINK_PATTERN.search(link)
        if not match:
            logger.warning("could not extract MD ID from RSS link: %s", link)
            continue

        listings.append(MDListing(mcd_id=match.group(1), title=title, html_url=link))

    return listings


def parse_md_html(body: bytes, *, mcd_id: str, html_url: str) -> MesoscaleDiscussion:
    """Parse a single MD's HTML page into a MesoscaleDiscussion."""
    text = _extract_pre_text(body.decode("utf-8", errors="replace"))

    return MesoscaleDiscussion(
        mcd_id=mcd_id,
        title=_extract_first_line_containing(text, "Mesoscale Discussion"),
        areas=_extract_field(text, "Areas affected"),
        concerning=_extract_field(text, "Concerning"),
        valid_raw=_extract_valid(text),
        summary=_extract_field(text, "SUMMARY", multiline=True),
        polygon=_parse_lat_lon_polygon(text),
        html_url=html_url,
    )


def fetch_active_mds(client: SPCClient) -> Optional[list[MesoscaleDiscussion]]:
    """Fetch RSS, then each listed MD's HTML, returning fully-parsed MDs.

    Returns:
        None  — RSS hash unchanged since last call (caller can skip MD work)
        []    — RSS fresh, but no MDs in effect right now
        list  — one entry per active MD whose HTML parsed successfully
    """
    try:
        rss_body = client.fetch_md_rss()
    except FetchError as e:
        logger.warning("failed to fetch MD RSS: %s", e)
        return []  # treat as "no MDs found this cycle"
    if rss_body is None:
        return None

    listings = parse_rss(rss_body)
    if not listings:
        return []

    parsed: list[MesoscaleDiscussion] = []
    for listing in listings:
        try:
            html = client.fetch_md_html(listing.html_url)
            md = parse_md_html(html, mcd_id=listing.mcd_id, html_url=listing.html_url)
        except (FetchError, ValueError) as e:
            logger.warning("failed to fetch/parse MD %s: %s", listing.mcd_id, e)
            continue
        parsed.append(md)

    return parsed


# --- internal parsing helpers ---

def _extract_pre_text(html: str) -> str:
    m = re.search(r"<pre>(.*?)</pre>", html, re.DOTALL | re.IGNORECASE)
    if not m:
        raise ValueError("MD HTML has no <pre> block")
    return m.group(1)


def _extract_first_line_containing(text: str, needle: str) -> str:
    for line in text.splitlines():
        line = line.strip()
        if needle in line:
            return line
    return ""


def _extract_field(text: str, label: str, *, multiline: bool = False) -> Optional[str]:
    """Extract a 'Label...content' field from MD text."""
    if multiline:
        # SUMMARY...content can span multiple lines, terminated by another all-caps
        # field (like DISCUSSION...) or by the LAT...LON block.
        pattern = rf"\b{re.escape(label)}\.\.\.([\s\S]*?)(?=\n\s*(?:[A-Z][A-Z]+\.\.\.|LAT\.\.\.LON))"
    else:
        pattern = rf"\b{re.escape(label)}\.\.\.([^\n]+)"
    m = re.search(pattern, text)
    if not m:
        return None
    value = re.sub(r"\s+", " ", m.group(1)).strip()
    return value if value else None


def _extract_valid(text: str) -> Optional[str]:
    """The 'Valid' line uses 'Valid TIMES' (single space), not the Label...content
    pattern that other fields use, so it needs its own extractor.
    """
    m = re.search(r"^\s*Valid\s+(\S.+)$", text, re.MULTILINE)
    if not m:
        return None
    return re.sub(r"\s+", " ", m.group(1)).strip() or None


def _parse_lat_lon_polygon(text: str) -> Polygon:
    m = re.search(r"LAT\.\.\.LON((?:\s+\d{8})+)", text)
    if not m:
        raise ValueError("MD text has no LAT...LON polygon block")

    codes = re.findall(r"\d{8}", m.group(1))
    if len(codes) < 3:
        raise ValueError(f"MD polygon has only {len(codes)} vertices; need at least 3")

    points = [_decode_lat_lon(c) for c in codes]
    # GeoJSON ordering: (lon, lat). Shapely closes the ring automatically if needed.
    return shp.Polygon([(lon, lat) for lat, lon in points])


def _decode_lat_lon(code: str) -> tuple[float, float]:
    lat = int(code[:4]) / 100.0
    lon_raw = int(code[4:])
    if lon_raw < 3000:
        lon = -(100.0 + lon_raw / 100.0)
    else:
        lon = -lon_raw / 100.0
    return lat, lon
