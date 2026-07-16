"""Shared price-outlier guard for offer ingestion.

A single scraped offer pointing at the wrong listing (accessory fuzzy-matched
to the phone's model number, wrong variant, decimal-shift) shows up as a price
wildly below the rest of the market for the same SKU — the Xiaomi 15T incident
was a 12,133 TL Maui Jim sunglasses listing (SKU "MJ0439S-003-15T-58") sitting
next to a 42,000 TL real offer, and `min(offers)` then poisoned
products.base_price, which every listing, filter, and the fiyat/performans
ranking read.

Rule (kept identical to frontend/src/lib/models/Product.ts filterOfferOutliers
so both ends of the pipeline agree): drop an offer priced under half the
median of the *other* offers for the same product. Comparing against the
median of the others keeps one bad row from dragging its own threshold down.
Needs 2+ offers; the maximum offer always survives, so the result is never
empty.
"""


def filter_price_outliers(offers, key="price"):
    if len(offers) < 2:
        return offers
    prices = [float(o[key]) for o in offers]
    kept = []
    for i, offer in enumerate(offers):
        others = sorted(p for j, p in enumerate(prices) if j != i)
        mid = len(others) // 2
        median = others[mid] if len(others) % 2 else (others[mid - 1] + others[mid]) / 2
        if prices[i] >= median * 0.5:
            kept.append(offer)
    return kept
