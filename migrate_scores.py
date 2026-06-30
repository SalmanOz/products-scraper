import logging
import os
import json
import mysql.connector
from dotenv import load_dotenv

load_dotenv()

def migrate_teknoskor():
    db = mysql.connector.connect(
        host=os.getenv("DB_HOST"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
        database=os.getenv("DB_NAME"),
        port=os.getenv("DB_PORT")
    )
    cursor = db.cursor(dictionary=True)
    
    logging.info("📊 Migrating Teknoskor scores based on partials average...")
    
    cursor.execute("SELECT id, attributes FROM products")
    rows = cursor.fetchall()
    
    for row in rows:
        try:
            attrs = json.loads(row['attributes'])
            partials = attrs.get('partials', {})
            
            if partials:
                # Handle potential key typos in existing data
                scores = []
                for k in ['camera', 'design', 'battery', 'hardware', 'connectivity', 'conectivity']:
                    v = partials.get(k)
                    if isinstance(v, (int, float)) and v > 0:
                        scores.append(v)
                
                if scores:
                    avg = sum(scores) / len(scores)
                    new_score = int(round(avg * 10))
                    
                    cursor.execute("UPDATE products SET teknoskor_score = %s WHERE id = %s", (new_score, row['id']))
                    logging.info(f"✅ Updated ID {row['id']} -> {new_score}")
        except Exception as e:
            logging.error(f"❌ Error on ID {row['id']}: {e}")
            
    db.commit()
    db.close()
    logging.info("✨ Migration complete.")

if __name__ == "__main__":
    migrate_teknoskor()
