import logging
import mysql.connector
import os
from dotenv import load_dotenv

load_dotenv()

def reset_database():
    try:
        db = mysql.connector.connect(
            host=os.getenv("DB_HOST"),
            user=os.getenv("DB_USER"),
            password=os.getenv("DB_PASSWORD"),
            database=os.getenv("DB_NAME"),
            port=os.getenv("DB_PORT")
        )
        cursor = db.cursor()

        logging.warning("⚠️  Resetting database...")
        
        # Disable foreign key checks to truncate safely
        cursor.execute("SET FOREIGN_KEY_CHECKS = 0;")
        
        logging.info("🗑️  Truncating product_offers...")
        cursor.execute("TRUNCATE TABLE product_offers;")
        
        logging.info("🗑️  Truncating products...")
        cursor.execute("TRUNCATE TABLE products;")
        
        # Re-enable foreign key checks
        cursor.execute("SET FOREIGN_KEY_CHECKS = 1;")
        
        db.commit()
        logging.info("✅ Database reset complete. Ready for fresh scraping.")
        
        cursor.close()
        db.close()
    except Exception as e:
        logging.error(f"❌ Error resetting database: {str(e)}")

if __name__ == "__main__":
    reset_database()
