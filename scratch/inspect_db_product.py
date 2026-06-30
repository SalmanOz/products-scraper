import mysql.connector
import os
import json
from dotenv import load_dotenv

# load env from project's scraper_python directory
load_dotenv('/Users/salman/projects/products/scraper_python/.env')

def inspect_product(slug):
    db = mysql.connector.connect(
        host=os.getenv("DB_HOST"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
        database=os.getenv("DB_NAME"),
        port=os.getenv("DB_PORT")
    )
    cursor = db.cursor(dictionary=True)
    cursor.execute("SELECT name, slug, teknoskor_score, images, attributes FROM products WHERE slug = %s", (slug,))
    product = cursor.fetchone()
    cursor.close()
    db.close()
    
    if not product:
        print(f"❌ Product with slug {slug} not found.")
        return
        
    print(f"=== INSPECTING: {product['name']} ===")
    print(f"Slug: {product['slug']}")
    print(f"Teknoskor Score: {product['teknoskor_score']}")
    
    images = json.loads(product['images'])
    print(f"Images count: {len(images)}")
    for img in images[:3]:
        print(f"  - {img}")
        
    attrs = json.loads(product['attributes'])
    print(f"Attributes keys: {list(attrs.keys())}")
    print(f"Quick Specs: {attrs.get('quick_specs')}")
    print(f"RAM GB: {attrs.get('ram_gb')}")
    print(f"Storage GB: {attrs.get('storage_gb')}")
    print(f"Battery: {attrs.get('battery_mah')} mAh")
    print(f"FAQ count: {len(attrs.get('faq', []))}")
    print(f"Gaming Performance count: {len(attrs.get('gaming_performance', []))}")
    print(f"AnTuTu Score: {attrs.get('antutu_score')}")

if __name__ == "__main__":
    inspect_product("samsung-galaxy-s25-fe")
    print()
    inspect_product("apple-iphone-17")
