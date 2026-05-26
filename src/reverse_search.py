from __future__ import annotations
import os
import base64
import httpx
from dotenv import load_dotenv
from models import ProcessedImage

_SERPAPI_URL     = "https://serpapi.com/search.json"
_CATBOX_URL      = "https://litterbox.catbox.moe/resources/internals/api.php"
_DEFAULT_TIMEOUT  = 30.0
_DEFAULT_MAX_PAGES = 3


def _load_default_key() -> str | None:
    load_dotenv()
    return os.getenv("SERPAPI_KEY")


class SerpApiSearcher:
    """Reverse-image search backed by SerpAPI's Google Lens engine.

    Implements the ReverseSearchProvider protocol (see models.py).

    SerpAPI Google Lens only accepts image URLs, not direct file uploads.
    search() uploads the image to a temporary public host (litterbox.catbox.moe,
    72-hour retention, no auth required), fetches up to max_pages pages of visual
    matches, and merges them into a single response dict. marketplace_parser sees a
    larger candidate pool without any change to its filter logic or signature.

    The API key and HTTP client are injected via the constructor so the searcher
    can be unit-tested offline (pass an httpx.Client built on httpx.MockTransport).
    """

    def __init__(
        self,
        api_key: str | None = None,
        client: httpx.Client | None = None,
        timeout: float = _DEFAULT_TIMEOUT,
        max_pages: int = _DEFAULT_MAX_PAGES,
    ) -> None:
        # api_key=None means "fall back to the environment"; an explicit ""
        # is honoured as an (invalid) key so tests can exercise the guard.
        self._api_key  = api_key if api_key is not None else _load_default_key()
        self._client   = client
        self._timeout  = timeout
        self._max_pages = max_pages

    def search(self, image: ProcessedImage) -> dict:
        """Upload image, search Google Lens across up to max_pages, return merged dict.

        The returned dict has the same shape as a single SerpAPI response but with
        visual_matches[] containing all pages merged so downstream parsers are
        unaffected by the pagination detail.
        """
        self._validate_key()
        if self._client is not None:
            return self._fetch_all(image, self._client)
        with httpx.Client(timeout=self._timeout) as client:
            return self._fetch_all(image, client)

    def _validate_key(self) -> None:
        if not self._api_key:
            raise EnvironmentError("SERPAPI_KEY not set in .env")

    def _fetch_all(self, image: ProcessedImage, client: httpx.Client) -> dict:
        """Upload image, fetch first page, follow pagination, merge visual_matches."""
        url = self._upload(image, client)

        params = {"engine": "google_lens", "api_key": self._api_key, "url": url}
        resp = client.get(_SERPAPI_URL, params=params)
        self._check_response(resp)
        data = resp.json()

        all_matches = list(data.get("visual_matches", []))

        current = data
        for _ in range(self._max_pages - 1):
            next_url = current.get("serpapi_pagination", {}).get("next")
            if not next_url:
                break
            resp = client.get(next_url)
            if resp.status_code != 200:
                break
            current = resp.json()
            all_matches.extend(current.get("visual_matches", []))

        data["visual_matches"] = all_matches
        return data

    def _upload(self, image: ProcessedImage, client: httpx.Client) -> str:
        """Upload image bytes to a temporary public host; return the public URL."""
        image_bytes = base64.b64decode(image.encoded)
        fmt = image.format.lower()
        resp = client.post(
            _CATBOX_URL,
            data={"reqtype": "fileupload", "time": "72h"},
            files={"fileToUpload": (f"upload.{fmt}", image_bytes, f"image/{fmt}")},
        )
        if resp.status_code != 200:
            raise RuntimeError(f"Image upload failed ({resp.status_code}): {resp.text[:200]}")
        url = resp.text.strip()
        if not url.startswith("https://"):
            raise RuntimeError(f"Unexpected upload response: {url[:200]}")
        return url

    @staticmethod
    def _check_response(response: httpx.Response) -> None:
        if response.status_code != 200:
            raise RuntimeError(
                f"SerpAPI {response.status_code}: {response.text[:300]}"
            )
