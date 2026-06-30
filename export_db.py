import logging
import os
import json
import decimal
import datetime
import mysql.connector
from dotenv import load_dotenv

load_dotenv()

def escape_val(val):
    if val is None:
        return "NULL"
    if isinstance(val, (int, float, decimal.Decimal)):
        return str(val)
    if isinstance(val, (datetime.datetime, datetime.date)):
        return f"'{val}'"
    if isinstance(val, (dict, list)):
        # Dump json and escape single quotes and backslashes
        s = json.dumps(val, ensure_ascii=False)
        s = s.replace('\\', '\\\\').replace("'", "\\'")
        return f"'{s}'"
    if isinstance(val, str):
        s = val.replace('\\', '\\\\').replace("'", "\\'").replace('\n', '\\n').replace('\r', '\\r')
        return f"'{s}'"
    return f"'{str(val)}'"

def generate_backup(output_path):
    logging.info(f"📦 Generating Hostinger/cPanel compatible MySQL backup...")
    db = mysql.connector.connect(
        host=os.getenv("DB_HOST", "localhost"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
        database=os.getenv("DB_NAME"),
        port=int(os.getenv("DB_PORT", 3306))
    )
    cursor = db.cursor(dictionary=True)

    tables = [
        'brands', 'categories', 'products', 'product_offers', 
        'product_prices', 'users', 'saved_comparisons', 'scraper_logs'
    ]

    # Specific real columns for products to avoid virtual column 3105 errors
    products_real_cols = [
        'id', 'name', 'slug', 'brand_id', 'category_id', 'base_price', 
        'images', 'attributes', 'created_at', 'updated_at', 
        'teknoskor_score', 'score_metadata', 'status'
    ]

    with open(output_path, 'w', encoding='utf-8') as f:
        f.write("-- Teknoskor Production Database Backup\n")
        f.write(f"-- Generated: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write("-- MySQL 8.0+ Compatible with Virtual Columns Handled\n\n")
        f.write("SET FOREIGN_KEY_CHECKS=0;\n")
        f.write("SET SQL_MODE = 'NO_AUTO_VALUE_ON_ZERO';\n")
        f.write("SET time_zone = '+03:00';\n\n")

        for table in tables:
            logging.info(f"  -> Exporting table `{table}`...")
            f.write(f"\n-- --------------------------------------------------------\n")
            f.write(f"-- Table structure for table `{table}`\n")
            f.write(f"-- --------------------------------------------------------\n\n")
            f.write(f"DROP TABLE IF EXISTS `{table}`;\n")

            # Get CREATE TABLE
            cursor.execute(f"SHOW CREATE TABLE `{table}`")
            create_stmt = cursor.fetchone()['Create Table']
            f.write(f"{create_stmt};\n\n")

            # Get Data
            cursor.execute(f"SELECT * FROM `{table}`")
            rows = cursor.fetchall()
            if not rows:
                f.write(f"-- No data for table `{table}`\n\n")
                continue

            f.write(f"-- Dumping data for table `{table}`\n")

            if table == 'products':
                cols = products_real_cols
            else:
                cols = list(rows[0].keys())

            cols_str = ", ".join([f"`{col}`" for col in cols])
            
            # Write inserts in batches
            batch_size = 50
            for i in range(0, len(rows), batch_size):
                batch = rows[i:i+batch_size]
                values_list = []
                for row in batch:
                    vals = [escape_val(row[col]) for col in cols]
                    values_list.append("(" + ", ".join(vals) + ")")
                
                insert_stmt = f"INSERT INTO `{table}` ({cols_str}) VALUES\n" + ",\n".join(values_list) + ";\n"
                f.write(insert_stmt)
            f.write("\n")

        f.write("\nSET FOREIGN_KEY_CHECKS=1;\n")
        f.write("-- Backup Completed Successfully\n")

    cursor.close()
    db.close()
    file_size_kb = os.path.getsize(output_path) / 1024
    logging.info(f"✅ Backup created successfully: {output_path} ({file_size_kb:.2f} KB)")

if __name__ == "__main__":
    backup_file = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "teknoskor_production_backup.sql"))
    generate_backup(backup_file)
