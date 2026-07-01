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
    
    prefixes = [
        "products/apple-iphone-16-pro",
        "products/tecno-camon-40",
        "products/apple-iphone-11"
    ]
    
    for prefix in prefixes:
        print(f"\n--- Checking Prefix: {prefix} ---")
        try:
            response = scraper.s3.list_objects_v2(Bucket=scraper.bucket_name, Prefix=prefix, MaxKeys=10)
            contents = response.get('Contents', [])
            print(f"Objects found: {len(contents)}")
            for obj in contents:
                print(f" - Key: {obj['Key']} | Size: {obj['Size']} bytes | Last Modified: {obj['LastModified']}")
        except Exception as e:
            print(f"❌ Failed to list prefix '{prefix}': {e}")

if __name__ == "__main__":
    inspect_bucket()
