"""
Kalshi REST client — RSA-PSS signed requests per Kalshi v2 API spec.
Clean slate version with full order management.
"""
import os, time, base64, json, httpx
from datetime import datetime, timezone
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

PROD_BASE = "https://api.elections.kalshi.com/trade-api/v2"
DEMO_BASE = "https://demo-api.kalshi.co/trade-api/v2"

class KalshiClient:
    def __init__(self):
        self.key_id   = os.getenv("KALSHI_API_KEY_ID", "").strip()
        pem_path      = os.getenv("KALSHI_PRIVATE_KEY_PATH", "/app/data/kalshi_private.pem")
        env           = os.getenv("KALSHI_ENV", "prod").lower()
        self.base     = PROD_BASE if env == "prod" else DEMO_BASE
        with open(pem_path, "rb") as f:
            self.private_key = serialization.load_pem_private_key(f.read(), password=None)
        self.http = httpx.Client(timeout=30.0)

    def _sign(self, ts_ms, method, path):
        msg = f"{ts_ms}{method.upper()}{path}".encode()
        sig = self.private_key.sign(
            msg,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256()
        )
        return base64.b64encode(sig).decode()

    def _headers(self, method, path):
        ts_ms = str(int(time.time() * 1000))
        return {
            "KALSHI-ACCESS-KEY":       self.key_id,
            "KALSHI-ACCESS-SIGNATURE": self._sign(ts_ms, method, path),
            "KALSHI-ACCESS-TIMESTAMP": ts_ms,
            "Content-Type":            "application/json",
        }

    def _request(self, method, path, params=None, data=None, retries=3):
        sign_path = "/trade-api/v2" + path
        url       = self.base + path
        headers   = self._headers(method, sign_path)
        for attempt in range(retries):
            try:
                r = self.http.request(method, url, params=params, json=data, headers=headers)
                if r.status_code == 429:
                    time.sleep(2 ** attempt)
                    continue
                if r.status_code >= 400:
                    raise RuntimeError(f"Kalshi {method} {path} -> {r.status_code}: {r.text[:300]}")
                return r.json()
            except httpx.TimeoutException:
                if attempt == retries - 1:
                    raise
                time.sleep(2)
        raise RuntimeError(f"Kalshi {method} {path} failed after {retries} retries")

    # ── Account ──────────────────────────────────────────
    def balance(self):
        return self._request("GET", "/portfolio/balance")

    def positions(self, limit=200):
        return self._request("GET", "/portfolio/positions", params={"limit": limit})

    # ── Markets ───────────────────────────────────────────
    def get_markets(self, series_ticker=None, status="open", cursor=None, limit=200):
        params = {"status": status, "limit": limit}
        if series_ticker: params["series_ticker"] = series_ticker
        if cursor:        params["cursor"]         = cursor
        return self._request("GET", "/markets", params=params)

    def get_market(self, ticker):
        return self._request("GET", f"/markets/{ticker}")

    # ── Orders ────────────────────────────────────────────
    def create_order(self, ticker, side, action, count,
                     type_="limit", yes_price=None, no_price=None,
                     client_order_id=None):
        body = {"ticker": ticker, "side": side, "action": action,
                "count": count, "type": type_}
        if yes_price is not None:    body["yes_price"]        = yes_price
        if no_price is not None:     body["no_price"]         = no_price
        if client_order_id:          body["client_order_id"]  = client_order_id
        return self._request("POST", "/portfolio/orders", data=body)

    def cancel_order(self, order_id):
        return self._request("DELETE", f"/portfolio/orders/{order_id}")

    def get_orders(self, status="resting", limit=200):
        return self._request("GET", "/portfolio/orders", params={"status": status, "limit": limit})
