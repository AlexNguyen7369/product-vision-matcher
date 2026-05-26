from __future__ import annotations
import os
import base64
import httpx
from dotenv import load_dotenv
from models import ProcessedImage

_SERPAPI_URL = "https://serpapi.com/search"
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
        """Send a ProcessedImage to SerpAPI Google Lens and return the raw dict.

        The full SerpAPI payload is returned unchanged so marketplace_parser
        can inspect every field; nothing is discarded at the network boundary.
        """
        self._validate_key()
        response = self._post(image)
        self._check_response(response)
        return response.json()

    def _validate_key(self) -> None:
        if not self._api_key:
            raise EnvironmentError("SERPAPI_KEY not set in .env")

    def _post(self, image: ProcessedImage) -> httpx.Response:
        image_bytes = base64.b64decode(image.encoded)
        fmt = image.format.lower()
        data = {"engine": "google_lens", "api_key": self._api_key}
        files = {"image": (f"upload.{fmt}", image_bytes, f"image/{fmt}")}

        # Reuse an injected client when provided (tests, connection pooling);
        # otherwise open a short-lived client scoped to this single request.
        if self._client is not None:
            return self._client.post(_SERPAPI_URL, data=data, files=files)
        with httpx.Client(timeout=self._timeout) as client:
            return client.post(_SERPAPI_URL, data=data, files=files)

    @staticmethod
    def _check_response(response: httpx.Response) -> None:
        if response.status_code != 200:
            raise RuntimeError(
                f"SerpAPI {response.status_code}: {response.text[:300]}"
            )
