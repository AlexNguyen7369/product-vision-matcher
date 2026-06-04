from __future__ import annotations

import base64
import os
from datetime import datetime, timezone
from urllib.parse import quote

import httpx

from models import KeywordSignal, SoldSignal, VolumeSignal

# Modern, non-deprecated eBay Buy APIs.
_OAUTH_URL    = "https://api.ebay.com/identity/v1/oauth2/token"
_BROWSE_BASE  = "https://api.ebay.com/buy/browse/v1"
_SCOPE        = "https://api.ebay.com/oauth/api_scope"   # public scope; client-credentials grant
_MARKETPLACE  = "EBAY_US"

# Seed queries used to discover candidate items via Best Match search. Best Match
# ordering is eBay's relevance/popularity ranking, so an item's position within a
# seed query is treated as its trending rank. Override via the constructor.
DEFAULT_SEED_QUERIES = [
    "electronics", "sneakers", "trading cards", "video games",
    "watches", "collectibles", "home", "toys",
]


class EbayTrendingProvider:
    """Trending-items backend backed by the eBay **Browse API** (modern REST).

    Implements the TrendingProvider protocol (see models.py). Auth uses the
    OAuth 2.0 *client-credentials* grant (application token) — only
    ``EBAY_CLIENT_ID`` + ``EBAY_CLIENT_SECRET`` are required; no RuName / user
    consent is needed for public Browse search.

    Signals (all from Browse, no special access):
      - keyword/rank  ← ``item_summary/search`` Best Match position per seed query
      - sold volume   ← ``getItem`` ``estimatedSoldQuantity``
      - sell-through  ← sold / (sold + ``estimatedAvailableQuantity``)

    Credentials and the HTTP client are injected so the provider can be
    unit-tested offline with an ``httpx.MockTransport`` — no network in tests.
    """

    def __init__(
        self,
        client_id: str | None = None,       # None → fall back to EBAY_CLIENT_ID env var
        client_secret: str | None = None,   # None → fall back to EBAY_CLIENT_SECRET env var
        client: httpx.Client | None = None,
        timeout: float = 30.0,
        max_results: int = 10,              # results pulled per seed query
        seed_queries: list[str] | None = None,
        marketplace: str = _MARKETPLACE,
    ) -> None:
        self._client_id     = client_id
        self._client_secret = client_secret
        self._client        = client
        self._timeout       = timeout
        self._max_results   = max_results
        self._seeds         = seed_queries or DEFAULT_SEED_QUERIES
        self._marketplace   = marketplace
        self._token: str | None = None
        # Memoize getItem responses so volume + sold signals share one call each.
        self._item_cache: dict[str, dict | None] = {}

    # ── protocol methods ──────────────────────────────────────────────────────

    def fetch_keyword_signals(self, lookback_days: int) -> list[KeywordSignal]:
        """Search each seed query (Best Match); list position = trending rank.

        Items appearing under more than one seed keep their best (lowest) rank.
        """
        self._validate_key()
        now = datetime.now(tz=timezone.utc)
        best: dict[str, KeywordSignal] = {}

        for seed in self._seeds:
            data = self._get(
                f"{_BROWSE_BASE}/item_summary/search",
                params={"q": seed, "limit": str(self._max_results)},
            )
            for pos, item in enumerate(data.get("itemSummaries") or [], start=1):
                item_id = item.get("itemId", "")
                if not item_id:
                    continue
                if item_id not in best or pos < best[item_id].rank:
                    best[item_id] = KeywordSignal(
                        item_id    = item_id,
                        keyword    = seed,
                        rank       = pos,
                        fetched_at = now,
                        title      = item.get("title", ""),
                        url        = item.get("itemWebUrl", ""),
                    )
        return list(best.values())

    def fetch_volume_signals(
        self, item_ids: list[str], lookback_days: int
    ) -> list[VolumeSignal]:
        """getItem per candidate → estimatedSoldQuantity (units sold)."""
        self._validate_key()
        now = datetime.now(tz=timezone.utc)
        signals: list[VolumeSignal] = []
        for item_id in item_ids:
            detail = self._item_detail(item_id)
            if detail is None:
                continue
            sold, _avail = self._quantities(detail)
            signals.append(VolumeSignal(
                item_id       = item_id,
                title         = detail.get("title", ""),
                sold_quantity = sold,
                fetched_at    = now,
            ))
        return signals

    def fetch_sold_signals(
        self, item_ids: list[str], lookback_days: int
    ) -> list[SoldSignal]:
        """getItem per candidate → sell-through = sold / (sold + available)."""
        self._validate_key()
        now = datetime.now(tz=timezone.utc)
        signals: list[SoldSignal] = []
        for item_id in item_ids:
            detail = self._item_detail(item_id)
            if detail is None:
                continue
            sold, avail = self._quantities(detail)
            total = sold + avail
            signals.append(SoldSignal(
                item_id     = item_id,
                title       = detail.get("title", ""),
                sold_count  = sold,
                total_count = total,
                sold_rate   = (sold / total) if total > 0 else 0.0,
                last_sold   = None,   # Browse exposes no per-sale timestamps
                fetched_at  = now,
            ))
        return signals

    # ── internals ─────────────────────────────────────────────────────────────

    def _resolved(self, value: str | None, env_var: str) -> str:
        return os.environ.get(env_var, "") if value is None else value

    def _resolved_client_id(self) -> str:
        return self._resolved(self._client_id, "EBAY_CLIENT_ID")

    def _resolved_client_secret(self) -> str:
        return self._resolved(self._client_secret, "EBAY_CLIENT_SECRET")

    def _validate_key(self) -> None:
        if not self._resolved_client_id() or not self._resolved_client_secret():
            raise EnvironmentError("EBAY_CLIENT_ID / EBAY_CLIENT_SECRET not set in .env")

    def _get_token(self) -> str:
        """Mint (and cache) an application access token via client-credentials."""
        if self._token:
            return self._token
        self._validate_key()
        creds = base64.b64encode(
            f"{self._resolved_client_id()}:{self._resolved_client_secret()}".encode()
        ).decode()
        data = self._request(
            "POST", _OAUTH_URL,
            data={"grant_type": "client_credentials", "scope": _SCOPE},
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Authorization": f"Basic {creds}",
            },
        )
        token = data.get("access_token")
        if not token:
            raise RuntimeError(f"eBay OAuth: no access_token in response: {str(data)[:300]}")
        self._token = token
        return token

    def _item_detail(self, item_id: str) -> dict | None:
        """getItem with per-instance memoization; None if the item 404s/errors."""
        if item_id in self._item_cache:
            return self._item_cache[item_id]
        try:
            detail = self._get(f"{_BROWSE_BASE}/item/{quote(item_id, safe='')}")
        except RuntimeError:
            detail = None
        self._item_cache[item_id] = detail
        return detail

    @staticmethod
    def _quantities(detail: dict) -> tuple[int, int]:
        """Pull (sold, available) from the first estimatedAvailabilities entry."""
        avails = detail.get("estimatedAvailabilities") or []
        first = avails[0] if avails else {}
        sold  = int(first.get("estimatedSoldQuantity") or 0)
        avail = int(first.get("estimatedAvailableQuantity") or 0)
        return sold, avail

    def _get(self, url: str, params: dict | None = None) -> dict:
        return self._request(
            "GET", url, params=params,
            headers={
                "Authorization": f"Bearer {self._get_token()}",
                "X-EBAY-C-MARKETPLACE-ID": self._marketplace,
            },
        )

    def _request(self, method: str, url: str, **kwargs) -> dict:
        if self._client:
            resp = self._client.request(method, url, **kwargs)
        else:
            with httpx.Client(timeout=self._timeout) as c:
                resp = c.request(method, url, **kwargs)
        if resp.status_code != 200:
            raise RuntimeError(f"eBay {resp.status_code}: {resp.text[:300]}")
        return resp.json()
