import logging
import os
import re
import json
import mysql.connector
from dotenv import load_dotenv

load_dotenv()

def cleanup_ratings():
    db = mysql.connector.connect(
        host=os.getenv("DB_HOST"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
        database=os.getenv("DB_NAME"),
        port=os.getenv("DB_PORT")
    )
    cursor = db.cursor(dictionary=True)
    
    logging.info("🧹 Cleaning up jammed ratings in database...")
    
    cursor.execute("SELECT id, attributes FROM products WHERE attributes LIKE '%Class A B C D E%'")
    rows = cursor.fetchall()
    
    for row in rows:
        try:
            attrs = json.loads(row['attributes'])
            modified = False
            
            # Look for jammed classes in all sections
            for section, specs in attrs.items():
                if isinstance(specs, dict):
                    for key, val in specs.items():
                        if isinstance(val, str) and "Class" in val and "A B C D E" in val:
                            # Since we can't know the selection from the jammed string, 
                            # we'll at least clean it up to "Class --" or try to find if there's any marker
                            # If we can't find it, we'll mark it as unknown or just Class C (average) 
                            # But better to just remove the junk and let the next scrape fix it properly.
                            # For now, let's just clear the junk string.
                            specs[key] = "---" 
                            modified = True
            
            if modified:
                cursor.execute("UPDATE products SET attributes = %s WHERE id = %s", (json.dumps(attrs), row['id']))
                logging.info(f"✅ Cleaned ID: {row['id']}")
        except Exception as e:
            logging.error(f"❌ Error on ID {row['id']}: {e}")
            
    db.commit()
    db.close()
    logging.info("✨ Cleanup complete. New scrapes will now use the correct logic.")

if __name__ == "__main__":
    cleanup_ratings()
