"""Minimal Polymarket US REST client (the CFTC-regulated US venue, not the international site).

Public market data comes from the gateway and needs no key. Trading calls read the
key from the environment, never from code or chat:

    POLYMARKET_US_KEY_ID       the key id from polymarket.us/developer
    POLYMARKET_US_SECRET_KEY   the base64 secret shown once at key creation
    POLYMARKET_US_ALLOW_LIVE   must be "1" before any order is sent (there is no demo exchange)

Requests are signed per the Polymarket US docs (Oct 2026): Ed25519 over
timestamp_ms + METHOD + path, base64-encoded; the private key is the first 32 bytes
of the decoded secret. Paths and fields follow docs.polymarket.us and need a check
with a tiny order before relying on them.
"""

import base64
import os
import time

import requests

GATEWAY_URL = "https://gateway.polymarket.us"
API_URL = "https://api.polymarket.us"


class LiveTradingNotAllowed(RuntimeError):
    pass


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
        self.key_id = os.environ.get("POLYMARKET_US_KEY_ID")
        self._private_key = None

    # --- auth -------------------------------------------------------------

    def _load_private_key(self):
        if self._private_key is None:
            from cryptography.hazmat.primitives.asymmetric import ed25519

            secret = os.environ.get("POLYMARKET_US_SECRET_KEY")
            if not secret or not self.key_id:
                raise RuntimeError("POLYMARKET_US_KEY_ID and POLYMARKET_US_SECRET_KEY must be set")
            self._private_key = ed25519.Ed25519PrivateKey.from_private_bytes(base64.b64decode(secret)[:32])
        return self._private_key

    def sign(self, timestamp_ms: str, method: str, path: str) -> str:
        message = f"{timestamp_ms}{method.upper()}{path.split('?')[0]}".encode()
        return base64.b64encode(self._load_private_key().sign(message)).decode()

    def _auth_headers(self, method: str, path: str) -> dict:
        ts = str(int(time.time() * 1000))
        return {
            "X-PM-Access-Key": self.key_id,
            "X-PM-Timestamp": ts,
            "X-PM-Signature": self.sign(ts, method, path),
        }

    # --- transport --------------------------------------------------------

    def _request(self, method: str, base: str, path: str, auth: bool = False, **kwargs) -> dict:
        for attempt in range(6):
            headers = self._auth_headers(method, path) if auth else {}
            resp = self.session.request(method, base + path, headers=headers, timeout=30, **kwargs)
            if resp.status_code == 429:
                time.sleep(min(2**attempt * 0.5, 30))
                continue
            resp.raise_for_status()
            return resp.json() if resp.content else {}
        resp.raise_for_status()
        return resp.json()

    # --- public market data -----------------------------------------------

    def league_events(self, league_slug: str, limit: int = 100) -> list[dict]:
        events, offset = [], 0
        while True:
            page = self._request(
                "GET", GATEWAY_URL, f"/v2/leagues/{league_slug}/events", params={"limit": limit, "offset": offset}
            )
            batch = page.get("events", [])
            events.extend(batch)
            if len(batch) < limit:
                return events
            offset += limit

    def bbo(self, market_slug: str) -> dict:
        return self._request("GET", GATEWAY_URL, f"/v1/markets/{market_slug}/bbo").get("marketData", {})

    # --- authenticated ----------------------------------------------------

    def _check_live(self) -> None:
        if os.environ.get("POLYMARKET_US_ALLOW_LIVE") != "1":
            raise LiveTradingNotAllowed("Set POLYMARKET_US_ALLOW_LIVE=1 to send orders to Polymarket US")

    def create_order(self, market_slug: str, intent: str, price: float, quantity: float, post_only: bool = True) -> dict:
        """intent is ORDER_INTENT_BUY_LONG (buy YES) or ORDER_INTENT_BUY_SHORT (buy NO)."""
        self._check_live()
        body = {
            "marketSlug": market_slug,
            "type": "ORDER_TYPE_LIMIT",
            "price": {"value": f"{price:.2f}", "currency": "USD"},
            "quantity": quantity,
            "tif": "TIME_IN_FORCE_GOOD_TILL_CANCEL",
            "intent": intent,
            "participateDontInitiate": post_only,
            "manualOrderIndicator": "MANUAL_ORDER_INDICATOR_AUTOMATIC",
        }
        return self._request("POST", API_URL, "/v1/orders", auth=True, json=body)

    def get_order(self, order_id: str) -> dict:
        data = self._request("GET", API_URL, f"/v1/order/{order_id}", auth=True)
        return data.get("order", data)

    def cancel_order(self, order_id: str, market_slug: str) -> dict:
        return self._request("POST", API_URL, f"/v1/order/{order_id}/cancel", auth=True, json={"marketSlug": market_slug})
