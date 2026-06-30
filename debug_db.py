import logging
import mysql.connector
import os
from dotenv import load_dotenv

load_dotenv()

def debug_db():
    db = mysql.connector.connect(
        host=os.getenv("DB_HOST"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
        database=os.getenv("DB_NAME"),
        port=os.getenv("DB_PORT")
    )
    cursor = db.cursor(dictionary=True)
    
    logging.info("--- Categories ---")
    cursor.execute("SELECT * FROM categories")
    for row in cursor.fetchall():
        logging.info(row)
        
    logging.info("\n--- Products Count ---")
    cursor.execute("SELECT count(*) as count FROM products")
    logging.info(cursor.fetchone())
    
    logging.info("\n--- Sample Products ---")
    cursor.execute("SELECT p.name, p.brand_id, p.category_id, b.name as brand_name, c.name as cat_name FROM products p LEFT JOIN brands b ON p.brand_id = b.id LEFT JOIN categories c ON p.category_id = c.id LIMIT 5")
    for row in cursor.fetchall():
        logging.info(row)
    
    cursor.close()
    db.close()

if __name__ == "__main__":
    debug_db()
