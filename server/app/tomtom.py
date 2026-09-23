"""TomTom Routing API client, shared by the container app and the host scripts
(scripts/poll.py, scripts/backfill.py import this via sys.path).

Retry policy: one retry with 2s backoff on connection errors and 5xx ONLY.
Never retry a 4xx — a 403 means a bad or over-quota key, and hammering it
makes things worse.
"""

import hashlib
import json
import time

import httpx

BASE = "https://api.tomtom.com/routing/1/calculateRoute"
TIMEOUT = 15.0


class TomTomError(Exception):
    pass


def _locs(origin: tuple[float, float], dest: tuple[float, float],
          via: list[list[float]] | None) -> str:
    pts = [origin, *[(v[0], v[1]) for v in (via or [])], dest]
    return ":".join(f"{lat:.5f},{lon:.5f}" for lat, lon in pts)


def calculate_route(api_key: str, origin: tuple[float, float], dest: tuple[float, float],
                    *, via: list[list[float]] | None = None, max_alternatives: int = 0,
                    depart_at: str | None = None) -> dict:
    """One routing call. Returns the parsed JSON body.

    depart_at: ISO8601 local-offset timestamp. Setting a FUTURE time disables live
    traffic and returns durations from TomTom's historical speed profiles (per road,
    per weekday, 5-min bins) — this is what powers the day-one backfill.
    """
    params = {
        "key": api_key,
        "traffic": "true",
        "computeTravelTimeFor": "all",
        "sectionType": "traffic",
        "routeRepresentation": "polyline",
        "travelMode": "car",
    }
    if max_alternatives:
        params["maxAlternatives"] = str(max_alternatives)
    if depart_at:
        params["departAt"] = depart_at

    url = f"{BASE}/{_locs(origin, dest, via)}/json"
    last_err: Exception | None = None
    for attempt in (1, 2):
        try:
            resp = httpx.get(url, params=params, timeout=TIMEOUT)
            if resp.status_code >= 500:
                last_err = TomTomError(f"HTTP {resp.status_code}: {resp.text[:200]}")
            elif resp.status_code >= 400:
                raise TomTomError(f"HTTP {resp.status_code}: {resp.text[:200]}")
            else:
                return resp.json()
        except (httpx.ConnectError, httpx.ReadTimeout, httpx.ConnectTimeout) as e:
            last_err = e
        if attempt == 1:
            time.sleep(2)
    raise TomTomError(f"request failed after retry: {last_err}")


def extract_summary(route: dict) -> dict:
    s = route.get("summary", {})
    return {
        "travel_sec": s.get("travelTimeInSeconds"),
        "free_flow_sec": s.get("noTrafficTravelTimeInSeconds"),
        "historic_sec": s.get("historicTrafficTravelTimeInSeconds"),
        "delay_sec": s.get("trafficDelayInSeconds"),
        "length_meters": s.get("lengthInMeters"),
    }


def extract_points(route: dict) -> list[list[float]]:
    """Flatten legs[].points[] to [[lon,lat],...] (GeoJSON order), 5dp."""
    pts: list[list[float]] = []
    for leg in route.get("legs", []):
        for p in leg.get("points", []):
            pts.append([round(p["longitude"], 5), round(p["latitude"], 5)])
    return pts


def geom_hash(points: list[list[float]]) -> str:
    return hashlib.sha256(json.dumps(points, separators=(",", ":")).encode()).hexdigest()


def bbox(points: list[list[float]]) -> list[float]:
    lons = [p[0] for p in points]
    lats = [p[1] for p in points]
    return [min(lons), min(lats), max(lons), max(lats)]


def extract_sections(route: dict) -> list[dict]:
    """Compress traffic sections to [{"s","e","mag"}] point-index ranges."""
    out = []
    for sec in route.get("sections", []):
        if sec.get("sectionType") == "TRAFFIC":
            out.append({
                "s": sec.get("startPointIndex"),
                "e": sec.get("endPointIndex"),
                "mag": sec.get("magnitudeOfDelay", 0),
            })
    return out
