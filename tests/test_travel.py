"""Tests for the straight-line travel-time estimator."""

# Lazy annotations.
from __future__ import annotations

# Path for the required db_path field.
from pathlib import Path

# pytest for approx and raises.
import pytest

# Settings supplies the estimator's speed and road factor.
from setuhaul.config import Settings

# The module (patched in one test), then the functions and class under test.
from setuhaul import travel
from setuhaul.travel import StraightLineEstimator, estimate_travel_minutes, haversine_km


def test_haversine_known_distance() -> None:
    # One degree of latitude is about 111.19 km with a 6371 km Earth radius.
    assert haversine_km(0, 0, 1, 0) == pytest.approx(111.19, abs=0.1)


def test_zero_distance_is_zero_minutes() -> None:
    # Same point in and out means no travel time.
    assert estimate_travel_minutes(12.9, 77.6, 12.9, 77.6, avg_speed_kmph=40.0, road_factor=1.3) == 0


def test_estimate_rounds_up(monkeypatch: pytest.MonkeyPatch) -> None:
    # Pin the straight-line distance so the arithmetic is exact: 10 km at 40 kmph, factor 1.0, is exactly 15.0 min.
    monkeypatch.setattr(travel, "haversine_km", lambda *_a: 10.0)
    assert estimate_travel_minutes(0, 0, 0, 0, avg_speed_kmph=40.0, road_factor=1.0) == 15
    # A tiny excess over that boundary (15.00015 min) rounds up to 16.
    monkeypatch.setattr(travel, "haversine_km", lambda *_a: 10.0001)
    assert estimate_travel_minutes(0, 0, 0, 0, avg_speed_kmph=40.0, road_factor=1.0) == 16
    # The road factor scales the distance first: 80 km * 1.5 = 120 km road, 120 / 40 kmph = 3 h = exactly 180 min.
    monkeypatch.setattr(travel, "haversine_km", lambda *_a: 80.0)
    assert estimate_travel_minutes(0, 0, 0, 0, avg_speed_kmph=40.0, road_factor=1.5) == 180


def test_non_positive_speed_rejected() -> None:
    # A zero or negative speed cannot yield a travel time.
    with pytest.raises(ValueError):
        estimate_travel_minutes(0, 0, 1, 1, avg_speed_kmph=0, road_factor=1.3)


def test_straight_line_estimator_uses_settings_values() -> None:
    # Custom settings values must drive the estimate.
    settings = Settings(db_path=Path("x"), gps_avg_speed_kmph=20.0, gps_road_factor=2.0)
    est = StraightLineEstimator(settings)
    # The estimator must equal the function called with the same two values.
    expected = estimate_travel_minutes(0, 0, 1, 0, avg_speed_kmph=20.0, road_factor=2.0)
    assert est.estimate(0, 0, 1, 0) == expected
    # And it must differ from the default-settings result, proving the values are read.
    assert expected != StraightLineEstimator(Settings(db_path=Path("x"))).estimate(0, 0, 1, 0)
