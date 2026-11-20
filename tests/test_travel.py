"""Tests for the straight-line travel-time estimator."""

# Lazy annotations.
from __future__ import annotations

# Path for the required db_path field.
from pathlib import Path

# pytest for approx and raises.
import pytest

# Settings supplies the estimator's speed and road factor.
from setuhaul.config import Settings

# The functions and class under test.
from setuhaul.travel import StraightLineEstimator, estimate_travel_minutes, haversine_km


def test_haversine_known_distance() -> None:
    # One degree of latitude is about 111.19 km with a 6371 km Earth radius.
    assert haversine_km(0, 0, 1, 0) == pytest.approx(111.19, abs=0.1)


def test_zero_distance_is_zero_minutes() -> None:
    # Same point in and out means no travel time.
    assert estimate_travel_minutes(12.9, 77.6, 12.9, 77.6, avg_speed_kmph=40.0, road_factor=1.3) == 0


def test_estimate_rounds_up() -> None:
    # 60 km straight * 1.3 / 40 kmph = 1.95 h = 117 min exactly; pick a point 60 km away along the equator.
    lon = 60.0 / 111.19488  # degrees of longitude at the equator for 60 km
    minutes = estimate_travel_minutes(0, 0, 0, lon, avg_speed_kmph=40.0, road_factor=1.3)
    # ceil of ~117.0 is 117 (or 118 if float noise lands just above); allow only those via a tight range.
    assert minutes in (117, 118)
    # A clearly fractional case rounds up: 10 km * 1.0 / 40 kmph = 15 min; slightly more distance gives 16.
    assert estimate_travel_minutes(0, 0, 0, 10.01 / 111.19492, avg_speed_kmph=40.0, road_factor=1.0) == 16


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
