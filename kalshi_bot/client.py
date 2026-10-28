"""Minimal Kalshi REST client.

Public market data needs no credentials. Authenticated calls read the key from
the environment, never from code or chat:

    KALSHI_API_KEY_ID   the key id shown in Kalshi's API key settings
    KALSHI_PRIVATE_KEY  the PEM private key text (or KALSHI_PRIVATE_KEY_PATH)
    KALSHI_ENV          "demo" (default) or "prod"
    KALSHI_ALLOW_LIVE   must be "1" before any order is sent to prod

Requests are signed per Kalshi's docs: RSA-PSS/SHA-256 (salt = digest length)
over timestamp_ms + METHOD + path, where path includes /trade-api/v2 and
excludes the query string.
"""

import base64
import os
import time
import uuid
from urllib.parse import urlparse

import requests

BASE_URLS = {
    "prod": "https://external-api.kalshi.com/trade-api/v2",
    "demo": "https://external-api.demo.kalshi.co/trade-api/v2",
}


class LiveTradingNotAllowed(RuntimeError):
    pass


class KalshiClient:
    def __init__(self, env: str | None = None, session: requests.Session | None = None):
        self.env = env or os.environ.get("KALSHI_ENV", "demo")
        self.base_url = BASE_URLS[self.env]
        self.session = session or requests.Session()
        self.key_id = os.environ.get("KALSHI_API_KEY_ID")
        self._private_key = None
        self.min_interval = 0.1
        self._last_request = 0.0

    # --- auth -------------------------------------------------------------

    def _load_private_key(self):
        if self._private_key is None:
            from cryptography.hazmat.primitives import serialization

            pem = os.environ.get("KALSHI_PRIVATE_KEY")
            if not pem and os.environ.get("KALSHI_PRIVATE_KEY_PATH"):
                with open(os.environ["KALSHI_PRIVATE_KEY_PATH"], "rb") as f:
                    pem = f.read().decode()
            if not pem or not self.key_id:
                raise RuntimeError("KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY must be set")
            self._private_key = serialization.load_pem_private_key(pem.encode(), password=None)
        return self._private_key

    def sign(self, timestamp_ms: str, method: str, path: str) -> str:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ed25519, padding

        key = self._load_private_key()
        message = f"{timestamp_ms}{method.upper()}{path.split('?')[0]}".encode()
        if isinstance(key, ed25519.Ed25519PrivateKey):
            signature = key.sign(message)
        else:
            signature = key.sign(
                message,
                padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
                hashes.SHA256(),
            )
        return base64.b64encode(signature).decode()

    def _auth_headers(self, method: str, endpoint: str) -> dict:
        ts = str(int(time.time() * 1000))
        path = urlparse(self.base_url + endpoint).path
        return {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "KALSHI-ACCESS-SIGNATURE": self.sign(ts, method, path),
        }

    # --- transport --------------------------------------------------------

    def _throttle(self) -> None:
        # Basic-tier read limit is 20 requests/s; stay under it so bulk collection doesn't trip 429s.
        wait = self._last_request + self.min_interval - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.monotonic()

    def _request(self, method: str, endpoint: str, auth: bool = False, **kwargs) -> dict:
        headers = self._auth_headers(method, endpoint) if auth else {}
        for attempt in range(12):
            self._throttle()
            resp = self.session.request(method, self.base_url + endpoint, headers=headers, timeout=30, **kwargs)
            if resp.status_code == 429:
                retry_after = resp.headers.get("Retry-After")
                time.sleep(float(retry_after) if retry_after else min(2**attempt * 0.5, 60))
                if auth:
                    headers = self._auth_headers(method, endpoint)
                continue
            resp.raise_for_status()
            return resp.json()
        resp.raise_for_status()
        return resp.json()

    def paginate(self, endpoint: str, key: str, params: dict | None = None, limit: int = 1000):
        params = dict(params or {}, limit=limit)
        while True:
            page = self._request("GET", endpoint, params=params)
            yield from page.get(key, [])
            cursor = page.get("cursor")
            if not cursor:
                return
            params["cursor"] = cursor

    # --- public market data -----------------------------------------------

    def historical_cutoff(self) -> dict:
        return self._request("GET", "/historical/cutoff")

    def get_series(self, series_ticker: str) -> dict:
        return self._request("GET", f"/series/{series_ticker}")["series"]

    def get_event(self, event_ticker: str) -> dict:
        return self._request("GET", f"/events/{event_ticker}")

    def markets(self, historical: bool = False, **params):
        endpoint = "/historical/markets" if historical else "/markets"
        return self.paginate(endpoint, "markets", params)

    def trades(self, historical: bool = False, **params):
        endpoint = "/historical/trades" if historical else "/markets/trades"
        return self.paginate(endpoint, "trades", params)

    def orderbook(self, ticker: str, depth: int | None = None) -> dict:
        params = {"depth": depth} if depth else {}
        return self._request("GET", f"/markets/{ticker}/orderbook", params=params)

    # --- authenticated ----------------------------------------------------

    def balance(self) -> dict:
        return self._request("GET", "/portfolio/balance", auth=True)

    def positions(self) -> dict:
        return self._request("GET", "/portfolio/positions", auth=True)

    def create_order(
        self,
        ticker: str,
        side: str,
        count: float,
        price: float,
        time_in_force: str = "good_till_canceled",
        post_only: bool = True,
        client_order_id: str | None = None,
    ) -> dict:
        """side is "bid" (buy YES) or "ask" (sell YES, i.e. buy NO at 1 - price)."""
        if self.env == "prod" and os.environ.get("KALSHI_ALLOW_LIVE") != "1":
            raise LiveTradingNotAllowed("Set KALSHI_ALLOW_LIVE=1 to send orders to the live exchange")
        if side not in ("bid", "ask"):
            raise ValueError("side must be 'bid' or 'ask'")
        body = {
            "ticker": ticker,
            "side": side,
            "count": f"{count:.2f}",
            "price": f"{price:.4f}",
            "time_in_force": time_in_force,
            "self_trade_prevention_type": "maker",
            "post_only": post_only,
            "client_order_id": client_order_id or str(uuid.uuid4()),
        }
        return self._request("POST", "/portfolio/events/orders", auth=True, json=body)

    def cancel_order(self, order_id: str) -> dict:
        return self._request("DELETE", f"/portfolio/orders/{order_id}", auth=True)
