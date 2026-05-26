from __future__ import annotations
import os
import base64
import httpx
from dotenv import load_dotenv
from image_processor import ProcessedImage

load_dotenv()
SERPAPI_KEY = os.getenv("SERPAPI_KEY")

_SERPAPI_URL = "https://serpapi.com/search"
_TIMEOUT     = 30.0


def search(image: ProcessedImage) -> dict:
    """
    Send a ProcessedImage to SerpAPI Google Lens and return the raw response dict.

    The returned dict is passed directly to marketplace_parser.parse().
    The caller receives the full SerpAPI payload so nothing is discarded
    before marketplace_parser has a chance to inspect it.
    """
    _validate_key()
    response = _post(image)
    _check_response(response)
    return response.json()


def _validate_key() -> None:
    if not SERPAPI_KEY:
        raise EnvironmentError("SERPAPI_KEY not set in .env")


def _post(image: ProcessedImage) -> httpx.Response:
    image_bytes = base64.b64decode(image.encoded)
    mime = f"image/{image.format.lower()}"
    with httpx.Client(timeout=_TIMEOUT) as client:
        return client.post(
            _SERPAPI_URL,
            data={"engine": "google_lens", "api_key": SERPAPI_KEY},
            files={"image": (f"upload.{image.format.lower()}", image_bytes, mime)},
        )


def _check_response(response: httpx.Response) -> None:
    if response.status_code != 200:
        raise RuntimeError(
            f"SerpAPI {response.status_code}: {response.text[:300]}"
        )
