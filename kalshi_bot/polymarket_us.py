"""Read-only Polymarket US market data (the CFTC-regulated US venue, not the international site).

Uses the public gateway, which needs no API key. Paths and fields follow
docs.polymarket.us as of Oct 2026. This client has no order methods on purpose:
the sports tool only suggests plays, and you place any bet yourself.
"""

import time

import requests

GATEWAY_URL = "https://gateway.polymarket.us"


def amount(value) -> float | None:
    """Read an Amount object ({"value": "0.55", "currency": "USD"}) as dollars."""
    if isinstance(value, dict):
        value = value.get("value")
    if value in (None, ""):
        return None
    return float(value)


class PolymarketUSClient:
    def __init__(self, session: requests.Session | None = None):
        self.session = session or requests.Session()

    def _get(self, path: str, **params) -> dict:
        for attempt in range(6):
            resp = self.session.get(GATEWAY_URL + path, params=params, timeout=30)
            if resp.status_code == 429:
                time.sleep(min(2**attempt * 0.5, 30))
                continue
            resp.raise_for_status()
            return resp.json()
        resp.raise_for_status()
        return resp.json()

    def league_events(self, league_slug: str, limit: int = 100) -> list[dict]:
        events, offset = [], 0
        while True:
            batch = self._get(f"/v2/leagues/{league_slug}/events", limit=limit, offset=offset).get("events", [])
            events.extend(batch)
            if len(batch) < limit:
                return events
            offset += limit

    def bbo(self, market_slug: str) -> dict:
        return self._get(f"/v1/markets/{market_slug}/bbo").get("marketData", {})
