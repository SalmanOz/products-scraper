"""One-shot repair for base_price rows poisoned by unfiltered min(offers).

For every published product this script:
  1. loads its current offers,
  2. drops price outliers (price_sanity.filter_price_outliers — same rule the
     ingestion now applies, so this is exactly what a clean re-scrape would
     have produced) and deletes those rows,
  3. recomputes base_price = min(surviving offers) and fixes it if it drifted
     (covers both outlier poisoning and stale base_price left behind by
     since-deleted offers).

Products with zero offer rows are left untouched: an empty offers table can't
distinguish "never scraped" from "nothing found", and zeroing prices here
would hide products from every price-filtered listing.

Default is a dry run that only prints what would change. Run with --apply to
write. Uses the same .env (DB_HOST/DB_USER/DB_PASSWORD/DB_NAME/DB_PORT) as
update_prices.py.
"""

import argparse
import logging
import os

import mysql.connector
from dotenv import load_dotenv

from price_sanity import filter_price_outliers

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(message)s")


def run(apply: bool):
    db = mysql.connector.connect(
        host=os.getenv("DB_HOST"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
        database=os.getenv("DB_NAME"),
        port=int(os.getenv("DB_PORT", 3306)),
    )
    cursor = db.cursor(dictionary=True)

    cursor.execute("SELECT id, name, slug, base_price FROM products WHERE status = 'published'")
    products = cursor.fetchall()

    outliers_removed = 0
    prices_fixed = 0
    skipped_no_offers = 0

    for p in products:
        cursor.execute(
            "SELECT id, merchant_name, price, affiliate_url FROM product_offers WHERE product_id = %s",
            (p["id"],),
        )
        offers = cursor.fetchall()
        if not offers:
            skipped_no_offers += 1
            continue

        kept = filter_price_outliers(offers)
        dropped = [o for o in offers if o not in kept]

        for d in dropped:
            outliers_removed += 1
            logging.info(
                f"🚫 {p['name']}: outlier offer {d['merchant_name']} {d['price']} TL "
                f"({(d['affiliate_url'] or '')[:70]})"
            )
            if apply:
                cursor.execute("DELETE FROM product_offers WHERE id = %s", (d["id"],))

        new_base = min(float(o["price"]) for o in kept)
        old_base = float(p["base_price"] or 0)
        if abs(new_base - old_base) > 0.01:
            prices_fixed += 1
            logging.info(f"💰 {p['name']} ({p['slug']}): base_price {old_base:.2f} -> {new_base:.2f} TL")
            if apply:
                cursor.execute(
                    "UPDATE products SET base_price = %s, updated_at = CURRENT_TIMESTAMP WHERE id = %s",
                    (new_base, p["id"]),
                )

    if apply:
        db.commit()

    mode = "APPLIED" if apply else "DRY RUN (nothing written — rerun with --apply)"
    logging.info(
        f"\n{mode}: {len(products)} products checked, {outliers_removed} outlier offers removed, "
        f"{prices_fixed} base_price values fixed, {skipped_no_offers} skipped (no offers)."
    )
    cursor.close()
    db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    run(parser.parse_args().apply)
