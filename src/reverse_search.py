from __future__ import annotations
import os
import base64
import httpx
from dotenv import load_dotenv
from models import ProcessedImage

_SERPAPI_URL = "https://serpapi.com/search.json"
_CATBOX_URL  = "https://litterbox.catbox.moe/resources/internals/api.php"
_DEFAULT_TIMEOUT = 30.0


def _load_default_key() -> str | None:
    load_dotenv()
    return os.getenv("SERPAPI_KEY")


class SerpApiSearcher:
    """Reverse-image search backed by SerpAPI's Google Lens engine.

    Implements the ReverseSearchProvider protocol (see models.py). The API key
    and HTTP client are injected through the constructor rather than read from
    module-level globals, so the searcher can be configured per-instance and
    unit-tested offline (pass an httpx.Client built on httpx.MockTransport).

    SerpAPI Google Lens only accepts image URLs, not direct file uploads.
    search() therefore first uploads the image to a temporary public host
    (litterbox.catbox.moe, 72-hour retention, no auth required), then passes
    the returned URL to SerpAPI.
    """

    def __init__(
        self,
        api_key: str | None = None,
        client: httpx.Client | None = None,
        timeout: float = _DEFAULT_TIMEOUT,
    ) -> None:
        # api_key=None means "fall back to the environment"; an explicit ""
        # is honoured as an (invalid) key so tests can exercise the guard.
        self._api_key = api_key if api_key is not None else _load_default_key()
        self._client = client
        self._timeout = timeout

    def search(self, image: ProcessedImage) -> dict:
        """Upload the image, search with Google Lens, return the raw response dict.

        The full SerpAPI payload is returned unchanged so marketplace_parser
        can inspect every field; nothing is discarded at the network boundary.
        """
        self._validate_key()
        response = self._search(image)
        self._check_response(response)
        return response.json()

    def _validate_key(self) -> None:
        if not self._api_key:
            raise EnvironmentError("SERPAPI_KEY not set in .env")

    def _upload(self, image: ProcessedImage) -> str:
        """Upload image bytes to a temporary public host; return the public URL."""
        image_bytes = base64.b64decode(image.encoded)
        fmt = image.format.lower()
        resp = self._request("POST", _CATBOX_URL,
            data={"reqtype": "fileupload", "time": "72h"},
            files={"fileToUpload": (f"upload.{fmt}", image_bytes, f"image/{fmt}")},
        )
        if resp.status_code != 200:
            raise RuntimeError(f"Image upload failed ({resp.status_code}): {resp.text[:200]}")
        url = resp.text.strip()
        if not url.startswith("https://"):
            raise RuntimeError(f"Unexpected upload response: {url[:200]}")
        return url

    def _search(self, image: ProcessedImage) -> httpx.Response:
        url = self._upload(image)
        params = {"engine": "google_lens", "api_key": self._api_key, "url": url}
        return self._request("GET", _SERPAPI_URL, params=params)

    def _request(self, method: str, url: str, **kwargs) -> httpx.Response:
        """Issue an HTTP request, reusing an injected client when provided."""
        if self._client is not None:
            return self._client.request(method, url, **kwargs)
        with httpx.Client(timeout=self._timeout) as client:
            return client.request(method, url, **kwargs)

    @staticmethod
    def _check_response(response: httpx.Response) -> None:
        if response.status_code != 200:
            raise RuntimeError(
                f"SerpAPI {response.status_code}: {response.text[:300]}"
            )
