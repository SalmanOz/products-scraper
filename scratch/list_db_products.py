import mysql.connector
import os
from dotenv import load_dotenv

# load env from project's scraper_python directory
load_dotenv('/Users/salman/projects/products/scraper_python/.env')

def list_all():
    db = mysql.connector.connect(
        host=os.getenv("DB_HOST"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
        database=os.getenv("DB_NAME"),
        port=os.getenv("DB_PORT")
    )
    cursor = db.cursor(dictionary=True)
    cursor.execute("SELECT name FROM products ORDER BY name")
    db_products = cursor.fetchall()
    cursor.close()
    db.close()
    
    print(f"Total products in DB: {len(db_products)}")
    for p in db_products:
        print(f"- {p['name']}")

if __name__ == "__main__":
    list_all()
