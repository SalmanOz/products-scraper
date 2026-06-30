import logging
import os
import json
import mysql.connector
from re import sub
from dotenv import load_dotenv

load_dotenv()

def extract_number(text):
    if not text: return 0
    t = str(text).replace('\t', '').replace('\n', '').strip()
    if t.count('.') > 1: t = t.replace('.', '')
    match = sub(r'(\d+[\d,.]*)', '', t) # This logic was wrong in the scraper too, but let's fix it here
    import re
    match = re.search(r'(\d+[\d,.]*)', t)
    if match:
        val_str = match.group(1).replace(',', '')
        try: return float(val_str)
        except: return 0
    return 0

def calculate_gaming_performance(antutu, battery, nm):
    games = {
        "PUBG Mobile": {"icon": "🔫", "weight": 1.0, "max_fps": 120},
        "Genshin Impact": {"icon": "✨", "weight": 2.2, "max_fps": 60},
        "CoD: Warzone Mobile": {"icon": "🎖️", "weight": 1.8, "max_fps": 120},
        "EA FC Mobile": {"icon": "⚽", "weight": 1.2, "max_fps": 120},
        "Mobile Legends": {"icon": "⚔️", "weight": 0.8, "max_fps": 120},
        "Roblox": {"icon": "🧱", "weight": 0.7, "max_fps": 60}
    }
    
    performance_data = []
    nm = nm if nm > 0 else 6 
    
    for name, config in games.items():
        if antutu >= 1500000: fps = config["max_fps"]
        elif antutu >= 1000000: fps = int(config["max_fps"] * 0.85)
        elif antutu >= 700000: fps = int(config["max_fps"] * 0.60)
        elif antutu >= 400000: fps = int(config["max_fps"] * 0.40)
        else: fps = int(config["max_fps"] * 0.25)
        
        base_drain = config["weight"] * 800 
        efficiency_factor = 1 + (nm - 4) * 0.1 
        actual_drain = base_drain * efficiency_factor
        play_time = round(battery / actual_drain, 1)
        
        performance_data.append({
            "game": name,
            "icon": config["icon"],
            "fps": fps,
            "max_fps": config["max_fps"],
            "hours": play_time,
            "tier": "Ultra" if fps >= config["max_fps"] * 0.9 else "Yüksek" if fps >= config["max_fps"] * 0.7 else "Orta"
        })
    return performance_data

def bulk_update():
    db = mysql.connector.connect(
        host=os.getenv("DB_HOST"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
        database=os.getenv("DB_NAME"),
        port=os.getenv("DB_PORT")
    )
    cursor = db.cursor(dictionary=True)

    logging.info("🔄 Fetching all products...")
    cursor.execute("SELECT id, attributes FROM products")
    products = cursor.fetchall()

    for p in products:
        try:
            attrs = json.loads(p['attributes'])
            
            # Extract required values
            antutu = int(attrs.get('antutu_score', 0))
            battery = int(attrs.get('battery_mah', 0))
            
            # Find nm (might be nested)
            nm = 0
            if 'Hardware' in attrs and 'Nanometers' in attrs['Hardware']:
                nm_text = attrs['Hardware']['Nanometers']
                import re
                match = re.search(r'(\d+)', str(nm_text))
                if match: nm = int(match.group(1))
            
            # Generate gaming performance
            attrs['gaming_performance'] = calculate_gaming_performance(antutu, battery, nm)
            
            # Update DB
            cursor.execute("UPDATE products SET attributes = %s WHERE id = %s", (json.dumps(attrs), p['id']))
            logging.info(f"✅ Updated product ID: {p['id']}")
        except Exception as e:
            logging.error(f"❌ Error updating product {p['id']}: {str(e)}")

    db.commit()
    logging.info(f"🚀 Bulk update finished. {len(products)} products processed.")
    db.close()

if __name__ == "__main__":
    bulk_update()
