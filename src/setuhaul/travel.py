"""Straight-line travel-time estimation.

Great-circle distance, inflated by a road factor, divided by an average speed. No routing service is used.
"""

# Lazy annotations.
from __future__ import annotations

# math supplies the trigonometry and ceil.
import math

# Protocol lets Task 3 depend on the estimator shape, not the class.
from typing import Protocol

# Settings carries the speed and road-factor values.
from setuhaul.config import Settings

# Mean Earth radius in kilometres.
EARTH_RADIUS_KM = 6371.0


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in kilometres between two latitude/longitude points in degrees."""
    # Convert the two latitudes to radians.
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    # Latitude and longitude differences in radians.
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    # Haversine of the central angle.
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlmb / 2) ** 2
    # Clamp against float drift above 1 before the square root.
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(min(1.0, a)))


def estimate_travel_minutes(
    lat: float,
    lon: float,
    dest_lat: float,
    dest_lon: float,
    *,
    avg_speed_kmph: float,
    road_factor: float,
) -> int:
    """Whole minutes to drive from (lat, lon) to the destination, rounded up."""
    # A non-positive speed would divide by zero or give negative time.
    if avg_speed_kmph <= 0:
        raise ValueError("avg_speed_kmph must be positive")
    # Straight-line distance scaled up to approximate road distance.
    road_km = haversine_km(lat, lon, dest_lat, dest_lon) * road_factor
    # Hours at the average speed, converted to minutes and rounded up.
    return math.ceil(road_km / avg_speed_kmph * 60)


class TravelTimeEstimator(Protocol):
    """Anything that can turn two coordinates into whole minutes of travel."""

    def estimate(self, lat: float, lon: float, dest_lat: float, dest_lon: float) -> int:
        """Return estimated travel minutes between the two points."""
        ...


class StraightLineEstimator:
    """TravelTimeEstimator backed by haversine distance and the configured speed and road factor."""

    def __init__(self, settings: Settings) -> None:
        # Keep only the two values this estimator needs.
        self._avg_speed_kmph = settings.gps_avg_speed_kmph
        self._road_factor = settings.gps_road_factor

    def estimate(self, lat: float, lon: float, dest_lat: float, dest_lon: float) -> int:
        # Delegate to the pure function with the configured values.
        return estimate_travel_minutes(
            lat,
            lon,
            dest_lat,
            dest_lon,
            avg_speed_kmph=self._avg_speed_kmph,
            road_factor=self._road_factor,
        )
