from __future__ import annotations
import os, sys, tempfile
from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(BASE_DIR, "src"))

app = FastAPI(title="Product Vision Matcher")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}

@app.get("/")
def index():
    return FileResponse(os.path.join(BASE_DIR, "index.html"))

@app.post("/analyze")
async def analyze(image: UploadFile = File(...)):
    suffix = os.path.splitext(image.filename)[1].lower()
    if suffix not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"Unsupported format: {suffix}")

    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(await image.read())
        tmp_path = tmp.name

    try:
        import pipeline
        from reverse_search import SerpApiSearcher

        report = pipeline.run(tmp_path, SerpApiSearcher())

        return {
            "listing_count":     report.listing_count,
            "avg_listing_price": round(report.avg_listing_price, 2),
            "sold_count":        report.sold_count,
            "avg_sold_price":    round(report.avg_sold_price, 2),
            "currency":          report.currency,
            "listings": [
                {
                    "title":            l.title,
                    "price_raw":        l.price_raw,
                    "price_value":      l.price_value,
                    "source":           l.source,
                    "url":              l.url,
                    "similarity_score": l.similarity_score,
                    "sold_date":        str(l.sold_date) if l.sold_date else None,
                }
                for l in report.listings
            ],
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        os.unlink(tmp_path)


if __name__ == "__main__":
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=True)