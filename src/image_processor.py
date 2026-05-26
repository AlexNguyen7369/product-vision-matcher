from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from PIL import Image
import base64
import io

SUPPORTED_FORMATS = {"JPEG", "PNG", "WEBP"}
MAX_SIZE = (1024, 1024)

# everything downstream recieves ProcessedImage, not raw PIL or bytes, should be encoded to attach to SerpAPI request body
# load the image, resize, and validate against supported_formats, return the data as ProcessedImage

@dataclass
class ProcessedImage: 
    encoded: str
    format: str
    size: tuple[int, int]

def process_image(path: str) -> ProcessedImage:
    # 3 method calls, load the image, validate, and resize so it doesn't overflow the supported size (MAX_SIZE)
    image = _load(path)
    _validate(image)
    image = _resize(image)
    return ProcessedImage(
        encoded= _encoded(image),
        format= image.format or "JPEG",
        size= image.size
    )


def _load(path: str) -> Image.Image:
    return Image.open(Path(path))

def _validate(image: Image.Image) -> None:
    if image.format not in SUPPORTED_FORMATS:
        raise ValueError(f"Unsupported format")

def _resize(image: Image.Image) -> Image.Image:
    image.thumbnail(MAX_SIZE)
    return image

def _encoded(image: Image.Image) -> str:
    buffer = io.BytesIO()
    image.save(buffer, format=image.format or "JPEG")
    return base64.b64encode(buffer.getvalue()).decode("utf-8")