from __future__ import annotations
import os, tempfile
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

@app.route("/")
def index():
    return send_from_directory(BASE_DIR, "index.html")

@app.route("/analyze", methods=["POST"])
def analyze():
    if "image" not in request.files:
        return jsonify({"error": "No image file provided"}), 400

    file = request.files["image"]
    if file.filename == "":
        return jsonify({"error": "Empty filename"}), 400

    suffix = os.path.splitext(file.filename)[1].lower()
    if suffix not in (".jpg", ".jpeg", ".png", ".webp"):
        return jsonify({"error": f"Unsupported format: {suffix}"}), 400

    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        file.save(tmp.name)
        tmp_path = tmp.name

    try:
        import sys
        sys.path.insert(0, os.path.join(BASE_DIR, "src"))

        import pipeline
        from reverse_search import SerpApiSearcher

        report = pipeline.run(tmp_path, SerpApiSearcher())

        return jsonify({
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
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        os.unlink(tmp_path)


if __name__ == "__main__":
    app.run(debug=True, port=5000)