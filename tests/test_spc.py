"""Tests for the SPC parser and point-in-polygon evaluator. Uses synthetic GeoJSON
so the tests are fully deterministic and offline.
"""

import json

import pytest

from lookout.config import RiskLevel
from lookout.spc import (
    PROBABILITY_TO_RISK,
    FetchError,
    _extract_probability,
    evaluate_location,
    parse_categorical_outlook,
    parse_probabilistic_outlook,
)


def _feature(label: str, *, valid: str, expire: str, issue: str, polygon: list):
    """Build one SPC outlook GeoJSON feature."""
    return {
        "type": "Feature",
        "geometry": {"type": "Polygon", "coordinates": [polygon]},
        "properties": {
            "LABEL": label,
            "VALID_ISO": valid,
            "EXPIRE_ISO": expire,
            "ISSUE_ISO": issue,
        },
    }


def _outlook_json(features: list) -> bytes:
    return json.dumps({"type": "FeatureCollection", "features": features}).encode()


# A simple square covering ~Tornado Alley centered on Oklahoma.
OK_SQUARE = [[-100.0, 33.0], [-95.0, 33.0], [-95.0, 38.0], [-100.0, 38.0], [-100.0, 33.0]]
# A nested smaller square — represents a higher-risk polygon inside the larger one.
OK_INNER = [[-98.0, 34.5], [-96.5, 34.5], [-96.5, 36.5], [-98.0, 36.5], [-98.0, 34.5]]
ISSUE = "2026-05-04T12:30:00+00:00"
VALID_AFTERNOON = "2026-05-04T16:30:00+00:00"
EXPIRE_AFTERNOON = "2026-05-05T12:00:00+00:00"


def test_parse_categorical_sorts_polygons_by_severity():
    body = _outlook_json([
        _feature("SLGT", valid=VALID_AFTERNOON, expire=EXPIRE_AFTERNOON, issue=ISSUE, polygon=OK_INNER),
        _feature("MRGL", valid=VALID_AFTERNOON, expire=EXPIRE_AFTERNOON, issue=ISSUE, polygon=OK_SQUARE),
    ])
    o = parse_categorical_outlook(body, outlook_day=1)
    risks = [p.risk for p in o.polygons]
    assert risks == [RiskLevel.MRGL, RiskLevel.SLGT]


def test_parse_skips_unknown_label():
    body = _outlook_json([
        _feature("MRGL", valid=VALID_AFTERNOON, expire=EXPIRE_AFTERNOON, issue=ISSUE, polygon=OK_SQUARE),
        _feature("Predictability Too Low", valid=VALID_AFTERNOON, expire=EXPIRE_AFTERNOON, issue=ISSUE, polygon=OK_SQUARE),
    ])
    o = parse_categorical_outlook(body, outlook_day=1)
    assert [p.risk for p in o.polygons] == [RiskLevel.MRGL]


def test_parse_raises_when_no_usable_features():
    body = _outlook_json([
        _feature("Predictability Too Low", valid=VALID_AFTERNOON, expire=EXPIRE_AFTERNOON, issue=ISSUE, polygon=OK_SQUARE),
    ])
    with pytest.raises(FetchError):
        parse_categorical_outlook(body, outlook_day=4)


def test_target_date_for_afternoon_valid_is_same_day():
    body = _outlook_json([
        _feature("MRGL", valid="2026-05-04T16:30:00+00:00",
                 expire="2026-05-05T12:00:00+00:00",
                 issue="2026-05-04T12:30:00+00:00",
                 polygon=OK_SQUARE),
    ])
    o = parse_categorical_outlook(body, outlook_day=1)
    assert o.target_date.isoformat() == "2026-05-04"


def test_target_date_for_overnight_valid_is_prior_day():
    # VALID at 01:00Z May 5 belongs to the May 4 convective day.
    body = _outlook_json([
        _feature("MRGL", valid="2026-05-05T01:00:00+00:00",
                 expire="2026-05-05T12:00:00+00:00",
                 issue="2026-05-05T00:52:00+00:00",
                 polygon=OK_SQUARE),
    ])
    o = parse_categorical_outlook(body, outlook_day=1)
    assert o.target_date.isoformat() == "2026-05-04"


def test_evaluate_location_returns_highest_containing_risk():
    # Nested polygons (SLGT inside MRGL) — point inside SLGT must return SLGT, not MRGL.
    body = _outlook_json([
        _feature("MRGL", valid=VALID_AFTERNOON, expire=EXPIRE_AFTERNOON, issue=ISSUE, polygon=OK_SQUARE),
        _feature("SLGT", valid=VALID_AFTERNOON, expire=EXPIRE_AFTERNOON, issue=ISSUE, polygon=OK_INNER),
    ])
    o = parse_categorical_outlook(body, outlook_day=1)
    # (lat=35.5, lon=-97.0) is inside SLGT inner box.
    assert evaluate_location(o, lat=35.5, lon=-97.0) == RiskLevel.SLGT


def test_evaluate_location_returns_outer_risk_when_outside_inner():
    body = _outlook_json([
        _feature("MRGL", valid=VALID_AFTERNOON, expire=EXPIRE_AFTERNOON, issue=ISSUE, polygon=OK_SQUARE),
        _feature("SLGT", valid=VALID_AFTERNOON, expire=EXPIRE_AFTERNOON, issue=ISSUE, polygon=OK_INNER),
    ])
    o = parse_categorical_outlook(body, outlook_day=1)
    # Inside MRGL outer box but outside SLGT inner box.
    assert evaluate_location(o, lat=33.5, lon=-99.0) == RiskLevel.MRGL


def test_evaluate_location_returns_none_when_outside_all():
    body = _outlook_json([
        _feature("MRGL", valid=VALID_AFTERNOON, expire=EXPIRE_AFTERNOON, issue=ISSUE, polygon=OK_SQUARE),
    ])
    o = parse_categorical_outlook(body, outlook_day=1)
    # Pacific Ocean.
    assert evaluate_location(o, lat=0.0, lon=-150.0) is None


# --- D4-8 probabilistic ---

def _prob_feature(label: str, *, valid: str, issue: str, dn: int, polygon: list):
    return {
        "type": "Feature",
        "geometry": {"type": "Polygon", "coordinates": [polygon]},
        "properties": {
            "LABEL": label,
            "DN": dn,
            "VALID_ISO": valid,
            "EXPIRE_ISO": "2026-05-09T12:00:00+00:00",
            "ISSUE_ISO": issue,
        },
    }


D4_VALID = "2026-05-08T12:00:00+00:00"
D4_ISSUE = "2026-05-04T07:30:00+00:00"


def test_extract_probability_handles_label_formats():
    assert _extract_probability({"LABEL": "15"}) == 15
    assert _extract_probability({"LABEL": "15%"}) == 15
    assert _extract_probability({"LABEL": "30 %"}) == 30
    assert _extract_probability({"LABEL": "  45%  "}) == 45


def test_extract_probability_falls_back_to_dn():
    assert _extract_probability({"LABEL": "Predictability Too Low", "DN": 30}) == 30


def test_extract_probability_returns_none_for_sentinel():
    assert _extract_probability({"LABEL": "Predictability Too Low", "DN": 0}) is None
    assert _extract_probability({"LABEL": "Potential Too Low", "DN": 0}) is None


def test_probability_to_risk_mapping():
    assert PROBABILITY_TO_RISK[15] == RiskLevel.MRGL
    assert PROBABILITY_TO_RISK[30] == RiskLevel.SLGT
    assert PROBABILITY_TO_RISK[45] == RiskLevel.ENH
    assert PROBABILITY_TO_RISK[60] == RiskLevel.MDT


def test_parse_probabilistic_maps_percentages_and_sorts():
    body = _outlook_json([
        _prob_feature("30%", valid=D4_VALID, issue=D4_ISSUE, dn=30, polygon=OK_INNER),
        _prob_feature("15%", valid=D4_VALID, issue=D4_ISSUE, dn=15, polygon=OK_SQUARE),
    ])
    o = parse_probabilistic_outlook(body, outlook_day=4)
    risks = [p.risk for p in o.polygons]
    assert risks == [RiskLevel.MRGL, RiskLevel.SLGT]


def test_parse_probabilistic_only_predictability_too_low_returns_empty():
    body = _outlook_json([
        _prob_feature("Predictability Too Low", valid=D4_VALID, issue=D4_ISSUE, dn=0, polygon=OK_SQUARE),
    ])
    o = parse_probabilistic_outlook(body, outlook_day=4)
    assert o.polygons == []
    # Timing is still extracted from the sentinel so target_date works.
    assert o.target_date.isoformat() == "2026-05-08"


def test_parse_probabilistic_skips_sentinel_keeps_real_features():
    body = _outlook_json([
        _prob_feature("Predictability Too Low", valid=D4_VALID, issue=D4_ISSUE, dn=0, polygon=OK_SQUARE),
        _prob_feature("15%", valid=D4_VALID, issue=D4_ISSUE, dn=15, polygon=OK_SQUARE),
    ])
    o = parse_probabilistic_outlook(body, outlook_day=4)
    assert [p.risk for p in o.polygons] == [RiskLevel.MRGL]


def test_evaluate_location_works_on_probabilistic_outlook():
    body = _outlook_json([
        _prob_feature("15%", valid=D4_VALID, issue=D4_ISSUE, dn=15, polygon=OK_SQUARE),
        _prob_feature("30%", valid=D4_VALID, issue=D4_ISSUE, dn=30, polygon=OK_INNER),
    ])
    o = parse_probabilistic_outlook(body, outlook_day=4)
    # Inside inner box → SLGT (30% mapped); outside inner but inside outer → MRGL (15%).
    assert evaluate_location(o, lat=35.5, lon=-97.0) == RiskLevel.SLGT
    assert evaluate_location(o, lat=33.5, lon=-99.0) == RiskLevel.MRGL
    assert evaluate_location(o, lat=0.0, lon=-150.0) is None


def test_parse_probabilistic_unmapped_percentage_skipped():
    # 5% has no RiskLevel mapping (no D1-3 categorical analog) — skipped silently.
    body = _outlook_json([
        _prob_feature("5%", valid=D4_VALID, issue=D4_ISSUE, dn=5, polygon=OK_SQUARE),
        _prob_feature("15%", valid=D4_VALID, issue=D4_ISSUE, dn=15, polygon=OK_INNER),
    ])
    o = parse_probabilistic_outlook(body, outlook_day=4)
    assert [p.risk for p in o.polygons] == [RiskLevel.MRGL]
