"""Coinbase Advanced Trade API data provider (candles, products, accounts, orders)."""
from __future__ import annotations

import math
import random
import secrets
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from redline.core.types import Balance, Candle, OrderSide, PlannedOrder, ProductConstraints, SubmittedOrder

try:
    import jwt as _jwt
    from cryptography.hazmat.primitives.serialization import load_pem_private_key

    _HAS_JWT = True
except ImportError:
    _HAS_JWT = False

BASE_URL = "https://api.coinbase.com"
MAX_CANDLES_PER_CALL = 350
MAX_RETRIES = 5


def _build_jwt(api_key: str, api_secret: str, method: str, path: str) -> str:
    if not _HAS_JWT:
        raise RuntimeError("pyjwt[crypto] is required for Coinbase API auth")
    # Handle escaped newlines from env var
    pem = api_secret.replace("\\n", "\n").encode()
    private_key = load_pem_private_key(pem, password=None)
    now = int(time.time())
    payload = {
        "sub": api_key,
        "iss": "cdp",
        "nbf": now,
        "exp": now + 120,
        "uri": f"{method} api.coinbase.com{path}",
    }
    token = _jwt.encode(
        payload,
        private_key,
        algorithm="ES256",
        headers={"kid": api_key, "nonce": secrets.token_hex(16)},
    )
    return token


def _backoff_wait(attempt: int, base: float = 1.0, max_wait: float = 60.0) -> None:
    wait = min(base * (2**attempt) + random.uniform(0, 1.0), max_wait)
    time.sleep(wait)


class CoinbaseDataProvider:
    """Fetch candles, product constraints, and account balances from Coinbase."""

    def __init__(self, api_key: str, api_secret: str, mock: bool = False) -> None:
        self.api_key = api_key
        self.api_secret = api_secret
        self.mock = mock
        self._client = httpx.Client(base_url=BASE_URL, timeout=30.0)

    def _auth_headers(self, method: str, path: str) -> dict[str, str]:
        token = _build_jwt(self.api_key, self.api_secret, method, path)
        return {"Authorization": f"Bearer {token}"}

    def _get(self, path: str, params: dict | None = None) -> Any:
        for attempt in range(MAX_RETRIES):
            headers = self._auth_headers("GET", path)
            try:
                resp = self._client.get(path, headers=headers, params=params)
            except httpx.TransportError:
                if attempt == MAX_RETRIES - 1:
                    raise
                _backoff_wait(attempt)
                continue

            if resp.status_code == 429 or (500 <= resp.status_code < 600):
                if attempt == MAX_RETRIES - 1:
                    resp.raise_for_status()
                _backoff_wait(attempt)
                continue

            resp.raise_for_status()
            return resp.json()

        raise RuntimeError(f"Failed GET {path} after {MAX_RETRIES} retries")

    # ------------------------------------------------------------------
    # Candles
    # ------------------------------------------------------------------

    def fetch_candles(self, product_id: str, min_count: int = 250) -> list[Candle]:
        """Fetch >= min_count daily candles, paginating backward if needed."""
        if self.mock:
            return _mock_candles(product_id, min_count + 10)

        candles: list[Candle] = []
        end_ts = int(
            datetime.now(timezone.utc)
            .replace(hour=0, minute=0, second=0, microsecond=0)
            .timestamp()
        )

        while len(candles) < min_count:
            start_ts = end_ts - MAX_CANDLES_PER_CALL * 86400
            path = f"/api/v3/brokerage/products/{product_id}/candles"
            data = self._get(
                path,
                params={"start": str(start_ts), "end": str(end_ts), "granularity": "ONE_DAY"},
            )
            batch = _parse_candles(data.get("candles", []))
            if not batch:
                break

            existing_ts = {c.ts_utc for c in candles}
            new_batch = [c for c in batch if c.ts_utc not in existing_ts]
            candles.extend(new_batch)

            if not new_batch:
                break  # no progress; avoid infinite loop

            oldest = min(c.ts_utc for c in batch)
            end_ts = int(oldest.timestamp())

        candles.sort(key=lambda c: c.ts_utc)
        return candles

    # ------------------------------------------------------------------
    # Product constraints
    # ------------------------------------------------------------------

    def fetch_product(self, product_id: str) -> ProductConstraints:
        """Fetch product constraints (size increments, min order sizes)."""
        if self.mock:
            return _mock_product(product_id)

        path = f"/api/v3/brokerage/products/{product_id}"
        data = self._get(path)
        return ProductConstraints(
            product_id=product_id,
            base_increment=data.get("base_increment", "0.00000001"),
            base_min_size=data.get("base_min_size", "0.000016"),
            quote_increment=data.get("quote_increment", "0.01"),
            quote_min_size=data.get("quote_min_size", "1"),
        )

    def _post(self, path: str, body: dict) -> Any:
        for attempt in range(MAX_RETRIES):
            headers = self._auth_headers("POST", path)
            try:
                resp = self._client.post(path, headers=headers, json=body)
            except httpx.TransportError:
                if attempt == MAX_RETRIES - 1:
                    raise
                _backoff_wait(attempt)
                continue

            if resp.status_code == 429 or (500 <= resp.status_code < 600):
                if attempt == MAX_RETRIES - 1:
                    resp.raise_for_status()
                _backoff_wait(attempt)
                continue

            resp.raise_for_status()
            return resp.json()

        raise RuntimeError(f"Failed POST {path} after {MAX_RETRIES} retries")

    # ------------------------------------------------------------------
    # Accounts / balances
    # ------------------------------------------------------------------

    def fetch_balances(self) -> dict[str, Balance]:
        """Return {asset: Balance} for all accounts with non-zero balances."""
        if self.mock:
            return _mock_balances()

        data = self._get("/api/v3/brokerage/accounts")
        balances: dict[str, Balance] = {}
        for account in data.get("accounts", []):
            asset = account.get("currency", "")
            available = float(account.get("available_balance", {}).get("value", 0) or 0)
            hold = float(account.get("hold", {}).get("value", 0) or 0)
            if asset and (available > 0 or hold > 0):
                balances[asset] = Balance(asset=asset, available=available, hold=hold)
        return balances

    # ------------------------------------------------------------------
    # Order submission and polling (Phase 2)
    # ------------------------------------------------------------------

    def submit_order(self, run_id: str, order: PlannedOrder) -> SubmittedOrder:
        """Submit a market order; return a SubmittedOrder with status='pending'."""
        from redline.timeutil import utcnow_iso

        if self.mock:
            return _mock_submit_order(run_id, order)

        client_order_id = uuid.uuid4().hex

        if order.side == OrderSide.BUY:
            order_config = {"market_market_ioc": {"quote_size": f"{order.quote_size_usd:.2f}"}}
        else:
            order_config = {"market_market_ioc": {"base_size": f"{order.base_size:.8f}"}}

        body = {
            "client_order_id": client_order_id,
            "product_id": order.product_id,
            "side": order.side.value.upper(),
            "order_configuration": order_config,
        }

        data = self._post("/api/v3/brokerage/orders", body)

        if not data.get("success"):
            err = data.get("error_response", {}).get("message", "unknown error")
            raise RuntimeError(f"Order submission failed for {order.product_id}: {err}")

        order_id = (
            data.get("success_response", {}).get("order_id")
            or data.get("order_id", "")
        )

        return SubmittedOrder(
            order_id=order_id,
            client_order_id=client_order_id,
            run_id=run_id,
            product_id=order.product_id,
            side=order.side,
            base_size=order.base_size,
            quote_size_usd=order.quote_size_usd,
            status="pending",
            submitted_at=utcnow_iso(),
        )

    def get_order(self, order_id: str) -> dict:
        """Return the Coinbase order dict for the given order_id."""
        if self.mock:
            return _mock_get_order(order_id)

        path = f"/api/v3/brokerage/orders/historical/{order_id}"
        data = self._get(path)
        return data.get("order", {})


# ------------------------------------------------------------------
# Candle parsing
# ------------------------------------------------------------------


def _parse_candles(raw: list[dict]) -> list[Candle]:
    result = []
    for item in raw:
        try:
            ts = datetime.fromtimestamp(int(item["start"]), tz=timezone.utc)
            result.append(
                Candle(
                    ts_utc=ts,
                    open=float(item["open"]),
                    high=float(item["high"]),
                    low=float(item["low"]),
                    close=float(item["close"]),
                    volume=float(item["volume"]),
                )
            )
        except (KeyError, ValueError, TypeError):
            continue
    return result


# ------------------------------------------------------------------
# Mock helpers
# ------------------------------------------------------------------


def _mock_candles(product_id: str, count: int) -> list[Candle]:
    """Generate deterministic synthetic daily candles."""
    seed = 42 if "BTC" in product_id else 43
    rng = random.Random(seed)

    start_price = 50000.0 if "BTC" in product_id else 3000.0
    now_midnight = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )

    candles = []
    price = start_price
    for i in range(count, 0, -1):
        ts = now_midnight - timedelta(days=i)
        daily_return = rng.gauss(0.0, 0.03)
        price = max(price * math.exp(daily_return), 1.0)
        o = price * (1 + rng.uniform(-0.005, 0.005))
        h = price * (1 + abs(rng.uniform(0, 0.01)))
        lo = price * (1 - abs(rng.uniform(0, 0.01)))
        candles.append(
            Candle(ts_utc=ts, open=o, high=h, low=lo, close=price, volume=rng.uniform(1000, 5000))
        )

    return candles


def _mock_product(product_id: str) -> ProductConstraints:
    if "BTC" in product_id:
        return ProductConstraints(
            product_id=product_id,
            base_increment="0.00000001",
            base_min_size="0.000016",
            quote_increment="0.01",
            quote_min_size="1",
        )
    return ProductConstraints(
        product_id=product_id,
        base_increment="0.00000001",
        base_min_size="0.00022",
        quote_increment="0.01",
        quote_min_size="1",
    )


def _mock_balances() -> dict[str, Balance]:
    return {
        "USD": Balance(asset="USD", available=10000.0),
        "BTC": Balance(asset="BTC", available=0.0),
        "ETH": Balance(asset="ETH", available=0.0),
    }


def _mock_submit_order(run_id: str, order: PlannedOrder) -> SubmittedOrder:
    from redline.timeutil import utcnow_iso

    return SubmittedOrder(
        order_id=f"mock_{uuid.uuid4().hex[:8]}",
        client_order_id=uuid.uuid4().hex,
        run_id=run_id,
        product_id=order.product_id,
        side=order.side,
        base_size=order.base_size,
        quote_size_usd=order.quote_size_usd,
        status="pending",
        submitted_at=utcnow_iso(),
    )


def _mock_get_order(order_id: str) -> dict:
    from redline.timeutil import utcnow_iso

    return {"status": "FILLED", "last_fill_time": utcnow_iso(), "order_id": order_id}
