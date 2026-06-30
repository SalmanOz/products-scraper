import logging
import mysql.connector
import os
import json
from dotenv import load_dotenv

load_dotenv()

def check_images():
    db = mysql.connector.connect(
        host=os.getenv("DB_HOST"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
        database=os.getenv("DB_NAME"),
        port=os.getenv("DB_PORT")
    )
    cursor = db.cursor(dictionary=True)
    cursor.execute("SELECT name, images FROM products LIMIT 5")
    rows = cursor.fetchall()
    
    for row in rows:
        logging.info(f"Product: {row['name']}")
        images = json.loads(row['images'])
        logging.info(f"URLs: {images}")
        logging.info("-" * 20)
    
    cursor.close()
    db.close()

if __name__ == "__main__":
    check_images()
