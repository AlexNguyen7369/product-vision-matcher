from __future__ import annotations
import sys
from models import PriceReport, ReverseSearchProvider
import image_processor
import marketplace_parser
import price_aggregator


def run(image_path: str, searcher: ReverseSearchProvider) -> PriceReport:
    """End-to-end pipeline: image file on disk → PriceReport.

    Orchestrates four stages in sequence:
      1. image_processor  — load, validate, resize, base64-encode
      2. searcher.search  — reverse-image search via injected provider
      3. marketplace_parser.parse — extract and filter valid listings
      4. price_aggregator.aggregate — compute averages, sort, return PriceReport
    """
    processed = image_processor.process_image(image_path)
    raw       = searcher.search(processed)
    listings  = marketplace_parser.parse(raw)
    return price_aggregator.aggregate(listings)


def format_report(report: PriceReport) -> str:
    """Return a human-readable summary of a PriceReport."""
    c = report.currency
    lines = [
        "─" * 44,
        f"  Active listings  : {report.listing_count}",
        f"  Avg listing price: {c}{report.avg_listing_price:.2f}",
        f"  Sold listings    : {report.sold_count}",
        f"  Avg sold price   : {c}{report.avg_sold_price:.2f}",
        "─" * 44,
    ]
    if report.listings:
        lines.append("  All listings (cheapest first):")
        for listing in report.listings:
            tag = "[sold]" if listing.sold_date else "      "
            lines.append(
                f"    {tag} {listing.price_raw:>8}  {listing.source:<12}  {listing.title}"
            )
    else:
        lines.append("  No valid listings found.")
    return "\n".join(lines)


if __name__ == "__main__":
    from reverse_search import SerpApiSearcher

    if len(sys.argv) < 2:
        print("Usage: python pipeline.py <image_path>")
        sys.exit(1)

    report = run(sys.argv[1], SerpApiSearcher())
    print(format_report(report))
