
import logging
import asyncio
import mysql.connector
import os
import time
from dotenv import load_dotenv
from tr_price_scraper import TRPriceScraper

load_dotenv()

class PriceUpdater:
    def __init__(self):
        self.db = None
        self.cursor = None
        self.ensure_connection()
        self.price_scraper = TRPriceScraper()

    def ensure_connection(self, max_retries=5):
        try:
            if self.db and self.db.is_connected():
                self.db.ping(reconnect=True, attempts=3, delay=2)
                self.cursor = self.db.cursor(dictionary=True, buffered=True)
                return
        except Exception:
            pass

        logging.info("  🔄 Connecting/Reconnecting to MySQL database...")
        try:
            if self.db:
                self.db.close()
        except Exception:
            pass

        for attempt in range(1, max_retries + 1):
            try:
                self.db = mysql.connector.connect(
                    host=os.getenv("DB_HOST", "localhost"),
                    user=os.getenv("DB_USER"),
                    password=os.getenv("DB_PASSWORD"),
                    database=os.getenv("DB_NAME"),
                    port=int(os.getenv("DB_PORT", 3306)),
                    buffered=True,
                    connection_timeout=30
                )
                self.cursor = self.db.cursor(dictionary=True, buffered=True)
                logging.info(f"  ✅ DB connected (attempt {attempt}/{max_retries})")
                return
            except Exception as e:
                logging.warning(f"  ⚠️ DB connection attempt {attempt}/{max_retries} failed: {e}")
                if attempt < max_retries:
                    wait = 10 * attempt
                    logging.info(f"  ⏳ Retrying in {wait}s...")
                    time.sleep(wait)
                else:
                    logging.error(f"  ❌ All {max_retries} DB connection attempts failed.")
                    raise

    def get_all_products(self):
        self.ensure_connection()
        self.cursor.execute("SELECT id, name, slug, base_price FROM products WHERE status = 'published'")
        return self.cursor.fetchall()

    def update_product_offers(self, product_id, offers, current_base_price):
        if not offers:
            return
        
        # Deadlock protection with retry
        for i in range(3):
            try:
                self.ensure_connection()
                # Delete old offers
                self.cursor.execute("DELETE FROM product_offers WHERE product_id = %s", (product_id,))
                
                # Insert new offers
                for o in offers:
                    self.cursor.execute("""
                        INSERT INTO product_offers (product_id, merchant_name, price, affiliate_url, is_official)
                        VALUES (%s, %s, %s, %s, %s)
                    """, (product_id, o['merchant'], o['price'], o['url'], 0))
                
                # Update base_price in products table to the minimum offer price
                min_price = min([o['price'] for o in offers])
                self.cursor.execute("UPDATE products SET base_price = %s WHERE id = %s", (min_price, product_id))
                
                # If the lowest price has changed, log it to the price history table
                curr_price = float(current_base_price) if current_base_price is not None else 0.0
                new_price = float(min_price)
                if abs(curr_price - new_price) > 0.01:
                    logging.info(f"  📈 Price change detected! Old: {curr_price} TL, New: {new_price} TL. Logging to history.")
                    self.cursor.execute("""
                        INSERT INTO product_prices (product_id, price)
                        VALUES (%s, %s)
                    """, (product_id, min_price))
                
                self.db.commit()
                logging.info(f"  ✅ Updated {len(offers)} offers. Min Price: {min_price} TL")
                break
            except mysql.connector.errors.InternalError as e:
                if e.errno == 1213: # Deadlock
                    logging.warning(f"  ⚠️ Deadlock, retrying ({i+1}/3)...")
                    time.sleep(2)
                    continue
                raise e
            except Exception as e:
                logging.error(f"  ❌ Error updating DB: {str(e)}")
                try:
                    self.db.rollback()
                except Exception:
                    pass
                break

    async def run_update(self, product_id=None):
        self.ensure_connection()
        if product_id:
            self.cursor.execute("SELECT id, name, slug, base_price FROM products WHERE id = %s", (product_id,))
            products = self.cursor.fetchall()
        else:
            products = self.get_all_products()
            
        logging.info(f"🚀 Starting price update for {len(products)} products...")
        
        for p in products:
            name = p['name']
            # Remove brand prefixes for better search on TR sites
            clean_name = name.replace('Apple ', '').replace('Samsung ', '').replace('Xiaomi ', '')
            
            logging.info(f"\n🔍 Updating prices for: {name}")
            try:
                offers = await self.price_scraper.get_best_prices(clean_name)
                self.ensure_connection()
                if offers:
                    self.update_product_offers(p['id'], offers, p['base_price'])
                else:
                    # Clear base_price if no offers found to avoid stale data
                    self.cursor.execute("UPDATE products SET base_price = 0 WHERE id = %s", (p['id'],))
                    self.db.commit()
                    logging.warning(f"  ⚠️ No offers found for {name}. Base price reset.")
            except Exception as e:
                logging.error(f"  ❌ Error fetching prices for {name}: {str(e)}")
            
            # Small delay to avoid aggressive scraping
            await asyncio.sleep(1)

        logging.info("\n🏁 Price update completed!")
        try:
            self.cursor.close()
            self.db.close()
        except Exception:
            pass

if __name__ == "__main__":
    import sys
    target_id = sys.argv[1] if len(sys.argv) > 1 else None
    updater = PriceUpdater()
    asyncio.run(updater.run_update(target_id))
