import mysql.connector
import os
import re
from dotenv import load_dotenv

# load env from project's scraper_python directory
load_dotenv('/Users/salman/projects/products/scraper_python/.env')

candidates = [
    "Samsung Galaxy S25 FE", "Apple iPhone 17", "Apple iPhone 17 Pro", "Poco X8 Pro", 
    "Redmi Note 15 Pro", "Apple iPhone 17 Pro Max", "Apple iPhone 15", "Honor Magic8 Pro", 
    "Honor 600", "Redmi 15C", "Samsung Galaxy A56 5G", "Honor 600 Pro", 
    "Apple iPhone 16", "Tecno Spark Slim 5G", "Poco F8 Pro", "Samsung Galaxy S26 Ultra", 
    "Xiaomi 15T Pro", "Samsung Galaxy S25 Ultra", "Samsung Galaxy S25", "Poco X8 Pro Max", 
    "Samsung Galaxy S26", "Apple iPhone 17e", "Xiaomi 17 Pro Max", "Xiaomi 15T", 
    "Samsung Galaxy A17 5G", "Apple iPhone 16e", "Redmi Note 14 Pro", "OnePlus 15"
]

def clean_name(name):
    # normalize names: lowercase, alphanumeric
    return re.sub(r'[^a-z0-9]', '', name.lower())

def check():
    db = mysql.connector.connect(
        host=os.getenv("DB_HOST"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
        database=os.getenv("DB_NAME"),
        port=os.getenv("DB_PORT")
    )
    cursor = db.cursor(dictionary=True)
    cursor.execute("SELECT id, name, slug FROM products")
    db_products = cursor.fetchall()
    cursor.close()
    db.close()
    
    db_clean_names = {clean_name(p['name']): p for p in db_products}
    db_clean_slugs = {clean_name(p['slug']): p for p in db_products}
    
    existing = []
    missing = []
    
    for cand in candidates:
        cand_clean = clean_name(cand)
        match = None
        # Try direct clean name match
        if cand_clean in db_clean_names:
            match = db_clean_names[cand_clean]
        elif cand_clean in db_clean_slugs:
            match = db_clean_slugs[cand_clean]
        else:
            # Try substring matches
            for db_clean, p in db_clean_names.items():
                if db_clean in cand_clean or cand_clean in db_clean:
                    match = p
                    break
        
        if match:
            existing.append((cand, match['name']))
        else:
            missing.append(cand)
            
    print("--- EXISTING PRODUCTS ---")
    for cand, db_p in existing:
        print(f"Candidate '{cand}' matches DB '{db_p}'")
        
    print("\n--- MISSING PRODUCTS ---")
    for m in missing:
        print(f"'{m}'")

if __name__ == "__main__":
    check()
