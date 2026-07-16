import logging
import os
import json
import asyncio
import mysql.connector
from dotenv import load_dotenv
from tr_price_scraper import TRPriceScraper
from price_sanity import filter_price_outliers

load_dotenv()

async def bulk_update_prices():
    db = mysql.connector.connect(
        host=os.getenv("DB_HOST"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
        database=os.getenv("DB_NAME"),
        port=os.getenv("DB_PORT")
    )
    cursor = db.cursor(dictionary=True)
    
    # Instantiate scraper
    tr_scraper = TRPriceScraper()
    
    logging.info("🔄 Fetching products for price update...")
    # Fetch all published products
    cursor.execute("SELECT id, name FROM products WHERE status = 'published'")
    products = cursor.fetchall()
    logging.info(f"📊 Found {len(products)} products to update.")
    
    for product in products:
        product_id = product['id']
        product_name = product['name']
        
        logging.info(f"\n📦 Updating prices for: {product_name} (ID: {product_id})")
        
        try:
            # Get best prices from 21+ merchants
            offers = await tr_scraper.get_best_prices(product_name)
            
            if offers:
                # Wrong-listing guard — see price_sanity.py (Xiaomi 15T /
                # Maui Jim incident: unfiltered min() poisoned base_price)
                offers = filter_price_outliers(offers)

                # 1. Clear old offers
                cursor.execute("DELETE FROM product_offers WHERE product_id = %s", (product_id,))

                # 2. Insert new offers
                lowest_price = min([o['price'] for o in offers])
                for offer in offers:
                    cursor.execute("""
                        INSERT INTO product_offers (product_id, merchant_name, price, affiliate_url, is_official)
                        VALUES (%s, %s, %s, %s, %s)
                    """, (product_id, offer['merchant'], offer['price'], offer['url'], 0))
                
                # 3. Update base price in products table
                cursor.execute("UPDATE products SET base_price = %s, updated_at = CURRENT_TIMESTAMP WHERE id = %s", (lowest_price, product_id))
                db.commit()
                logging.info(f"✅ Success: Best price {lowest_price} ₺ from {len(offers)} sources.")
            else:
                logging.info(f"ℹ️ No prices found for {product_name}")
                # Optional: clear old offers if none found? (User's choice, usually better to keep old if not found)
                # cursor.execute("UPDATE products SET updated_at = CURRENT_TIMESTAMP WHERE id = %s", (product_id,))
                # db.commit()
            
            import time
            time.sleep(3) # Throttle to prevent process spikes
            
        except Exception as e:
            logging.error(f"❌ Error updating {product_name}: {str(e)}")
            continue
            
    db.close()
    logging.info("\n🚀 Bulk price update completed!")

if __name__ == "__main__":
    asyncio.run(bulk_update_prices())
