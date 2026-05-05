"""Tests for MD parsing, notifier MD routing, and cycle idempotency."""

from datetime import datetime
from typing import Optional

import pytest
import shapely.geometry as shp
from sqlmodel import SQLModel, create_engine, select

from lookout.config import (
    Location,
    LookoutConfig,
    NotificationRule,
    PollingConfig,
    ProductKind,
    ProductsConfig,
    RiskLevel,
)
from lookout.db import session as db_session
from lookout.loop import process_md_cycle
from lookout.md import (
    MesoscaleDiscussion,
    _decode_lat_lon,
    parse_md_html,
    parse_rss,
)
from lookout.models import MesoscaleDiscussionAlert
from lookout.notifier import Notifier, format_md, match_md_rules


# --- _decode_lat_lon ---

def test_decode_lat_lon_east_of_100():
    # From real MD 0644: 37099541 → 37.09°N, -95.41°W (NE Oklahoma)
    lat, lon = _decode_lat_lon("37099541")
    assert lat == pytest.approx(37.09)
    assert lon == pytest.approx(-95.41)


def test_decode_lat_lon_west_of_100():
    # 30002541 → 30.0°N, -125.41°W (lon "0254" + 100 = 125.41)
    lat, lon = _decode_lat_lon("30002541")
    assert lat == pytest.approx(30.0)
    assert lon == pytest.approx(-125.41)


def test_decode_lat_lon_at_exactly_100w():
    # 30000000 → 30.0°N, -100.0°W (boundary case)
    lat, lon = _decode_lat_lon("30000000")
    assert lat == pytest.approx(30.0)
    assert lon == pytest.approx(-100.0)


# --- parse_rss ---

NO_MDS_RSS = b"""<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0">
  <channel>
    <item>
      <title>SPC - No MDs are in effect as of Tue May 5 08:01:01 UTC 2026</title>
      <link>https://www.spc.noaa.gov/products/md/</link>
      <description>No Mesoscale Discussions are in effect.</description>
    </item>
  </channel>
</rss>"""


ACTIVE_MD_RSS = b"""<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0">
  <channel>
    <item>
      <title>SPC MD 0644</title>
      <link>https://www.spc.noaa.gov/products/md/md0644.html</link>
      <description>Severe potential...</description>
    </item>
    <item>
      <title>SPC MD 0645</title>
      <link>https://www.spc.noaa.gov/products/md/md0645.html</link>
      <description>Watch likely</description>
    </item>
  </channel>
</rss>"""


def test_parse_rss_no_mds():
    assert parse_rss(NO_MDS_RSS) == []


def test_parse_rss_two_active_mds():
    listings = parse_rss(ACTIVE_MD_RSS)
    assert [l.mcd_id for l in listings] == ["0644", "0645"]
    assert listings[0].html_url == "https://www.spc.noaa.gov/products/md/md0644.html"


# --- parse_md_html ---

# Real-format MD text content captured from a live MD (md0644 sample format).
# The polygon is a closed loop in NE Oklahoma / NW Arkansas / SW Missouri.
SAMPLE_MD_HTML = b"""<html><body><pre>

   Mesoscale Discussion 0644
   NWS Storm Prediction Center Norman OK
   0101 AM CDT Tue May 05 2026

   Areas affected...Northeast OK into northwest AR and far southwest MO

   Concerning...Severe potential...Watch unlikely

   Valid 050601Z - 050800Z

   Probability of Watch Issuance...20 percent

   SUMMARY...Strong to locally severe storms will remain possible
   overnight.

   DISCUSSION...An elevated storm cluster has recently shown some signs
   of organization across northeast OK.

   ..Mead.. 05/05/2026

   ...Please see www.spc.noaa.gov for graphic product...

   ATTN...WFO...TSA...SGF...LZK...

   LAT...LON   37099541 36769396 36279245 35969268 35609313 35379404
               35729464 36029506 36249525 36699553 37099541

   $$
</pre></body></html>"""


def test_parse_md_html_extracts_all_fields():
    md = parse_md_html(SAMPLE_MD_HTML, mcd_id="0644", html_url="http://example/md0644.html")
    assert md.mcd_id == "0644"
    assert "Mesoscale Discussion 0644" in md.title
    assert md.areas == "Northeast OK into northwest AR and far southwest MO"
    assert md.concerning is not None and "Severe potential" in md.concerning
    assert md.valid_raw == "050601Z - 050800Z"
    assert md.summary is not None and "Strong to locally severe storms" in md.summary


def test_parse_md_html_polygon_contains_expected_point():
    md = parse_md_html(SAMPLE_MD_HTML, mcd_id="0644", html_url="x")
    # Polygon covers NE OK / NW AR / SW MO. (-94.0, 36.0) is inside (NW Arkansas).
    point_inside = shp.Point(-94.0, 36.0)
    point_outside = shp.Point(-110.0, 40.0)  # somewhere in Utah
    assert md.polygon.intersects(point_inside)
    assert not md.polygon.intersects(point_outside)


def test_parse_md_html_polygon_is_closed():
    md = parse_md_html(SAMPLE_MD_HTML, mcd_id="0644", html_url="x")
    # Shapely auto-closes; ring should be valid and have area.
    assert md.polygon.is_valid
    assert md.polygon.area > 0


def test_parse_md_html_raises_when_no_polygon():
    body = b"<html><pre>Mesoscale Discussion 0001\n   Areas affected...test\n</pre></html>"
    with pytest.raises(ValueError, match="LAT...LON"):
        parse_md_html(body, mcd_id="0001", html_url="x")


# --- notifier MD routing ---

def _config_for_md(*, products_filter: Optional[list] = None) -> LookoutConfig:
    return LookoutConfig(
        user_agent_contact="t@example.com",
        alert_threshold=RiskLevel.MRGL,
        locations={"home": Location(lat=36.0, lon=-95.5)},
        notification_channels={"phone": "ntfys://x"},
        notification_rules=[
            NotificationRule(
                name="all", locations=["home"], channels=["phone"],
                products=products_filter,
            )
        ],
        products=ProductsConfig(),
        polling=PollingConfig(),
    )


def _md(mcd_id: str = "0644") -> MesoscaleDiscussion:
    polygon = shp.Polygon([(-96, 35), (-95, 35), (-95, 37), (-96, 37), (-96, 35)])
    return MesoscaleDiscussion(
        mcd_id=mcd_id,
        title=f"MD {mcd_id}",
        areas="Test area",
        concerning="Severe potential",
        valid_raw="050601Z - 050800Z",
        summary="Storms expected",
        polygon=polygon,
        html_url=f"http://example/md{mcd_id}.html",
    )


def test_match_md_rules_includes_when_no_products_filter():
    cfg = _config_for_md()
    targets = match_md_rules(cfg, "home")
    assert {t.channel_name for t in targets} == {"phone"}


def test_match_md_rules_respects_products_filter():
    cfg = _config_for_md(products_filter=[ProductKind.CONVECTIVE_OUTLOOK])
    assert match_md_rules(cfg, "home") == []


def test_match_md_rules_includes_when_md_in_products_filter():
    cfg = _config_for_md(products_filter=[ProductKind.MESOSCALE_DISCUSSION])
    targets = match_md_rules(cfg, "home")
    assert {t.channel_name for t in targets} == {"phone"}


def test_match_md_rules_skips_locations_not_in_rule():
    cfg = _config_for_md()
    assert match_md_rules(cfg, "elsewhere") == []


def test_format_md_includes_id_and_link_and_summary():
    md = _md("0644")
    title, body = format_md(md, "home")
    assert "0644" in title and "home" in title
    assert "Test area" in body
    assert "Severe potential" in body
    assert "Storms expected" in body
    assert md.html_url in body


# --- process_md_cycle: dispatch + idempotency ---

def _engine():
    eng = create_engine("sqlite://", echo=False)
    SQLModel.metadata.create_all(eng)
    return eng


NOW = datetime(2026, 5, 5, 12, 0)


def test_process_md_cycle_dispatches_for_covered_location(capsys):
    eng = _engine()
    cfg = _config_for_md()
    notifier = Notifier(cfg, dry_run=True)
    md = _md("0644")

    mds_processed, alerts = process_md_cycle(cfg, notifier, eng, [md], NOW)

    assert mds_processed == 1
    assert alerts == 1
    assert "[DRY-RUN MD]" in capsys.readouterr().out


def test_process_md_cycle_idempotent_does_not_realert(capsys):
    eng = _engine()
    cfg = _config_for_md()
    notifier = Notifier(cfg, dry_run=False)  # commit so idempotency persists
    md = _md("0644")

    process_md_cycle(cfg, notifier, eng, [md], NOW)
    capsys.readouterr()

    # Run again with the same MD — idempotency table should suppress.
    process_md_cycle(cfg, notifier, eng, [md], NOW)
    out = capsys.readouterr().out
    assert "[DRY-RUN MD]" not in out

    with db_session(eng) as sess:
        rows = sess.exec(select(MesoscaleDiscussionAlert)).all()
    assert len(rows) == 1


def test_process_md_cycle_skips_locations_outside_polygon(capsys):
    eng = _engine()
    cfg = LookoutConfig(
        user_agent_contact="t@example.com",
        alert_threshold=RiskLevel.MRGL,
        # Two locations: one inside the polygon, one in Utah.
        locations={
            "home": Location(lat=36.0, lon=-95.5),
            "elsewhere": Location(lat=40.0, lon=-110.0),
        },
        notification_channels={"phone": "ntfys://x"},
        notification_rules=[
            NotificationRule(name="all", locations=["home", "elsewhere"], channels=["phone"])
        ],
        products=ProductsConfig(),
        polling=PollingConfig(),
    )
    notifier = Notifier(cfg, dry_run=False)
    md = _md("0644")

    process_md_cycle(cfg, notifier, eng, [md], NOW)

    with db_session(eng) as sess:
        rows = sess.exec(select(MesoscaleDiscussionAlert)).all()
    assert {r.location_name for r in rows} == {"home"}


def test_process_md_cycle_dry_run_does_not_commit(capsys):
    eng = _engine()
    cfg = _config_for_md()
    notifier = Notifier(cfg, dry_run=True)
    md = _md("0644")

    process_md_cycle(cfg, notifier, eng, [md], NOW, dry_run=True)

    with db_session(eng) as sess:
        rows = sess.exec(select(MesoscaleDiscussionAlert)).all()
    assert rows == []
