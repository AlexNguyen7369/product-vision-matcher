from __future__ import annotations

import os
from datetime import datetime, timezone

import httpx

from models import KeywordSignal, SoldSignal, WatchSignal

_MERCH_URL   = "https://svcs.ebay.com/MerchandisingService"
_FINDING_URL = "https://svcs.ebay.com/services/search/FindingService/v1"


class EbayTrendingProvider:
    """Trending-items backend backed by eBay Merchandising + Finding APIs.

    Implements the TrendingProvider protocol (see models.py).
    app_id and HTTP client are injected so the provider can be unit-tested
    offline with an httpx.MockTransport — no network required in tests.
    """

    def __init__(
        self,
        app_id: str | None = None,
        client: httpx.Client | None = None,
        timeout: float = 30.0,
        max_results: int = 50,
    ) -> None:
        self._app_id     = app_id  # None → fall back to env var in _validate_key()
        self._client     = client
        self._timeout    = timeout
        self._max_results = max_results

    # ── protocol methods ──────────────────────────────────────────────────────

    def fetch_keyword_signals(self, lookback_days: int) -> list[KeywordSignal]:
        """Call getMostWatchedItems; treat list position as keyword rank."""
        self._validate_key()
        params = {
            "OPERATION-NAME":       "getMostWatchedItems",
            "SERVICE-VERSION":      "1.1.0",
            "CONSUMER-ID":          self._resolved_app_id(),
            "RESPONSE-DATA-FORMAT": "JSON",
            "maxResults":           str(self._max_results),
        }
        data = self._get(_MERCH_URL, params)
        now  = datetime.now(tz=timezone.utc)

        items = (
            data.get("getMostWatchedItemsResponse", [{}])[0]
                .get("itemRecommendations", [{}])[0]
                .get("item", [])
        )

        signals: list[KeywordSignal] = []
        for rank, item in enumerate(items, start=1):
            item_id = item.get("itemId", [""])[0] if isinstance(item.get("itemId"), list) else item.get("itemId", "")
            keyword = item.get("title", [""])[0] if isinstance(item.get("title"), list) else item.get("title", "")
            signals.append(KeywordSignal(
                item_id    = str(item_id),
                keyword    = str(keyword),
                rank       = rank,
                fetched_at = now,
            ))
        return signals

    def fetch_watch_signals(
        self, item_ids: list[str], lookback_days: int
    ) -> list[WatchSignal]:
        """Call getMostWatchedItems; extract watch counts."""
        self._validate_key()
        params = {
            "OPERATION-NAME":       "getMostWatchedItems",
            "SERVICE-VERSION":      "1.1.0",
            "CONSUMER-ID":          self._resolved_app_id(),
            "RESPONSE-DATA-FORMAT": "JSON",
            "maxResults":           str(self._max_results),
        }
        data = self._get(_MERCH_URL, params)
        now  = datetime.now(tz=timezone.utc)

        items = (
            data.get("getMostWatchedItemsResponse", [{}])[0]
                .get("itemRecommendations", [{}])[0]
                .get("item", [])
        )

        signals: list[WatchSignal] = []
        for item in items:
            item_id = item.get("itemId", [""])[0] if isinstance(item.get("itemId"), list) else item.get("itemId", "")
            title   = item.get("title", [""])[0]   if isinstance(item.get("title"), list)   else item.get("title", "")
            wc_raw  = item.get("watchCount", ["0"])[0] if isinstance(item.get("watchCount"), list) else item.get("watchCount", "0")
            signals.append(WatchSignal(
                item_id     = str(item_id),
                title       = str(title),
                watch_count = int(wc_raw),
                fetched_at  = now,
            ))
        return signals

    def fetch_sold_signals(
        self, item_ids: list[str], lookback_days: int
    ) -> list[SoldSignal]:
        """Call findCompletedItems for each candidate; compute sold_rate."""
        from datetime import timedelta

        self._validate_key()
        now      = datetime.now(tz=timezone.utc)
        end_from = (now - timedelta(days=lookback_days)).strftime("%Y-%m-%dT%H:%M:%S.000Z")

        signals: list[SoldSignal] = []
        keywords_to_query = item_ids[:20] if item_ids else ["electronics"]

        for keyword in keywords_to_query:
            params = {
                "OPERATION-NAME":          "findCompletedItems",
                "SERVICE-VERSION":         "1.13.0",
                "SECURITY-APPNAME":        self._resolved_app_id(),
                "RESPONSE-DATA-FORMAT":    "JSON",
                "keywords":                keyword,
                "itemFilter(0).name":      "EndTimeFrom",
                "itemFilter(0).value":     end_from,
                "itemFilter(1).name":      "SoldItemsOnly",
                "itemFilter(1).value":     "true",
                "paginationInput.entriesPerPage": "100",
            }
            try:
                data = self._get(_FINDING_URL, params)
            except RuntimeError:
                continue

            search_result = (
                data.get("findCompletedItemsResponse", [{}])[0]
                    .get("searchResult", [{}])[0]
            )
            raw_items = search_result.get("item", [])
            total  = int(search_result.get("@count", len(raw_items)))
            sold   = sum(
                1 for it in raw_items
                if (it.get("sellingStatus", [{}])[0].get("sellingState", [""])[0] == "EndedWithSales"
                    if isinstance(it.get("sellingStatus"), list)
                    else it.get("sellingStatus", {}).get("sellingState") == "EndedWithSales")
            )
            last_sold: datetime | None = None
            for it in raw_items:
                end_time_raw = (
                    it.get("listingInfo", [{}])[0].get("endTime", [""])[0]
                    if isinstance(it.get("listingInfo"), list)
                    else it.get("listingInfo", {}).get("endTime", "")
                )
                if end_time_raw:
                    try:
                        ts = datetime.fromisoformat(end_time_raw.replace("Z", "+00:00"))
                        if last_sold is None or ts > last_sold:
                            last_sold = ts
                    except ValueError:
                        pass

            signals.append(SoldSignal(
                item_id     = keyword,
                title       = keyword,
                sold_count  = sold,
                total_count = total,
                sold_rate   = sold / total if total > 0 else 0.0,
                last_sold   = last_sold,
                fetched_at  = now,
            ))

        return signals

    # ── internals ─────────────────────────────────────────────────────────────

    def _resolved_app_id(self) -> str:
        if self._app_id is None:
            return os.environ.get("EBAY_APP_ID", "")
        return self._app_id

    def _validate_key(self) -> None:
        if not self._resolved_app_id():
            raise EnvironmentError("EBAY_APP_ID not set in .env")

    def _get(self, url: str, params: dict) -> dict:
        if self._client:
            resp = self._client.get(url, params=params)
        else:
            with httpx.Client(timeout=self._timeout) as c:
                resp = c.get(url, params=params)
        if resp.status_code != 200:
            raise RuntimeError(f"eBay {resp.status_code}: {resp.text[:300]}")
        return resp.json()
