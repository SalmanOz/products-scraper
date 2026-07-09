"""One-off data repair for SEO-visible attribute bugs:

1. screen_size_inch corrupted by decimal stripping (667 -> 6.67, 61 -> 6.1)
2. US-format thousands separators in stored FAQ answers ("1,934,662" -> "1.934.662")
3. Fabricated FAQ claims: entries asserting a refresh rate / charging wattage /
   battery life for products whose attributes lack that data (e.g. "60Hz" on a
   120Hz phone whose screen_refresh_rate was never scraped)
4. Offers from untrusted gray-import/dropship sellers (e.g. "Wireless Source")
   left over from before the merchant whitelist existed; base_price is
   recomputed from the remaining trusted offers

Safe to re-run; only rows that actually change are updated.
"""
import logging
import os
import re
import json
import mysql.connector
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def normalize_inches(val):
    """667 -> 6.67, 61 -> 6.1; leaves sane values (<= 15") untouched."""
    try:
        v = float(val)
    except (TypeError, ValueError):
        return None
    if v <= 0:
        return None
    while v > 15:
        v /= 10
    return round(v, 2)


def fix_faq_number_format(faq):
    """Replace US thousands separators with Turkish dots inside FAQ answer strings."""
    changed = False
    if not isinstance(faq, list):
        return faq, changed
    for item in faq:
        if not isinstance(item, dict):
            continue
        answer = item.get("a")
        if isinstance(answer, str) and re.search(r"\d,\d{3}", answer):
            item["a"] = re.sub(r"(?<=\d),(?=\d{3})", ".", answer)
            changed = True
    return faq, changed


def drop_unbacked_faq_claims(faq, attrs):
    """Remove FAQ entries whose claims aren't backed by scraped attribute data."""
    changed = False
    if not isinstance(faq, list):
        return faq, changed
    kept = []
    for item in faq:
        if not isinstance(item, dict):
            kept.append(item)
            continue
        text = f"{item.get('q', '')} {item.get('a', '')}"
        fabricated = (
            (re.search(r"\d+\s*Hz", text, re.I) and not attrs.get("screen_refresh_rate")) or
            (re.search(r"\d+\s*W\b", text) and not attrs.get("charging_speed_w")) or
            (re.search(r"\d+\s*mAh", text, re.I) and not attrs.get("battery_mah"))
        )
        if fabricated:
            changed = True
        else:
            kept.append(item)
    return kept, changed


def purge_untrusted_offers(cursor):
    """Delete offers from sellers outside the trusted-merchant whitelist and
    recompute base_price from the surviving offers."""
    from tr_price_scraper import TRPriceScraper

    cursor.execute("SELECT id, product_id, merchant_name, price, affiliate_url FROM product_offers")
    offers = cursor.fetchall()
    trusted = TRPriceScraper.TRUSTED_MERCHANTS
    removed_products = set()
    removed = 0
    for o in offers:
        m = (o["merchant_name"] or "").lower()
        url = (o["affiliate_url"] or "").lower()
        untrusted = m == "google shopping" or not any(t in m for t in trusted)
        # Broken URLs: Google help/search/redirect pages instead of the store
        bad_url = (
            "support.google.com" in url
            or "policies.google.com" in url
            or "google.com/search" in url
            or "google.com.tr/search" in url
        )
        if untrusted or bad_url:
            reason = "untrusted seller" if untrusted else "broken Google URL"
            cursor.execute("DELETE FROM product_offers WHERE id = %s", (o["id"],))
            logging.info(f"🚷 Removed offer ({reason}): {o['merchant_name']} ({o['price']} TL, product {o['product_id']})")
            removed_products.add(o["product_id"])
            removed += 1

    for pid in removed_products:
        cursor.execute("SELECT MIN(price) AS mp FROM product_offers WHERE product_id = %s", (pid,))
        row = cursor.fetchone()
        new_base = row["mp"] if row and row["mp"] else 0
        cursor.execute("UPDATE products SET base_price = %s WHERE id = %s", (new_base, pid))
    return removed


def run():
    db = mysql.connector.connect(
        host=os.getenv("DB_HOST"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
        database=os.getenv("DB_NAME"),
        port=os.getenv("DB_PORT")
    )
    cursor = db.cursor(dictionary=True)

    cursor.execute("SELECT id, name, attributes FROM products")
    rows = cursor.fetchall()
    logging.info(f"🔍 Checking {len(rows)} products...")

    fixed = 0
    for row in rows:
        try:
            attrs = json.loads(row["attributes"] or "{}")
        except json.JSONDecodeError:
            continue

        modified = False

        current = attrs.get("screen_size_inch")
        normalized = normalize_inches(current)
        if normalized is not None and normalized != current:
            attrs["screen_size_inch"] = normalized
            logging.info(f"📐 {row['name']}: screen {current} -> {normalized}\"")
            modified = True

        if "faq" in attrs:
            attrs["faq"], faq_changed = fix_faq_number_format(attrs["faq"])
            if faq_changed:
                logging.info(f"🔢 {row['name']}: fixed FAQ number format")
                modified = True

            attrs["faq"], faq_pruned = drop_unbacked_faq_claims(attrs["faq"], attrs)
            if faq_pruned:
                logging.info(f"🧹 {row['name']}: removed fabricated FAQ claims")
                modified = True

        if modified:
            cursor.execute(
                "UPDATE products SET attributes = %s WHERE id = %s",
                (json.dumps(attrs, ensure_ascii=False), row["id"])
            )
            fixed += 1

    removed_offers = purge_untrusted_offers(cursor)

    db.commit()
    cursor.close()
    db.close()
    logging.info(f"🏁 Done. Fixed {fixed} products, removed {removed_offers} untrusted offers.")


if __name__ == "__main__":
    run()
