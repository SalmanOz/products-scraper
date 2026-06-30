import mysql.connector
import os
from dotenv import load_dotenv

load_dotenv()

def debug_db():
    db = mysql.connector.connect(
        host=os.getenv("DB_HOST", "127.0.0.1"),
        user=os.getenv("DB_USER", "root"),
        password=os.getenv("DB_PASSWORD", "rootpassword"),
        database=os.getenv("DB_NAME", "product_comparison"),
        port=os.getenv("DB_PORT", 3306)
    )
    cursor = db.cursor(dictionary=True)
    
    cursor.execute("SELECT COUNT(*) as count FROM product_offers")
    offers = cursor.fetchone()
    print(f"Total product offers: {offers['count']}")
    
    cursor.execute("SELECT id, name, base_price FROM products WHERE base_price > 0 LIMIT 10")
    products = cursor.fetchall()
    print(f"\nProducts with valid base_price ({len(products)}):")
    for p in products:
        print(f"- {p['name']}: {p['base_price']} TL")
        
    cursor.execute("SELECT p.name, COUNT(o.id) as offer_count FROM products p LEFT JOIN product_offers o ON p.id = o.product_id GROUP BY p.id HAVING offer_count > 0 LIMIT 10")
    offer_counts = cursor.fetchall()
    print(f"\nProducts and their offer counts:")
    for o in offer_counts:
        print(f"- {o['name']}: {o['offer_count']} offers")
    
    cursor.close()
    db.close()

if __name__ == "__main__":
    debug_db()
