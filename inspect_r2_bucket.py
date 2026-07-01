import os
import boto3
from botocore.client import Config
from main import KimovilScraper

def inspect_bucket():
    scraper = KimovilScraper()
    if not scraper.r2_enabled:
        print("❌ R2 is not enabled.")
        return
        
    print(f"Connecting to R2 bucket '{scraper.bucket_name}'...")
    try:
        response = scraper.s3.list_objects_v2(Bucket=scraper.bucket_name, MaxKeys=50)
        contents = response.get('Contents', [])
        print(f"Total objects retrieved: {len(contents)}")
        if not contents:
            print("⚠️ Bucket is completely empty!")
            return
            
        print("\n--- Object Keys in Bucket ---")
        for obj in contents:
            print(f" - Key: {obj['Key']} | Size: {obj['Size']} bytes | Last Modified: {obj['LastModified']}")
    except Exception as e:
        print(f"❌ Failed to list bucket contents: {e}")

if __name__ == "__main__":
    inspect_bucket()
