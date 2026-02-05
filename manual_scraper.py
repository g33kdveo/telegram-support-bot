import json
import os

class ManualScraper:
    def __init__(self, file_path="manual_products.json"):
        self.file_path = file_path

    def get_products(self):
        """
        Reads product data from a local JSON file instead of scraping.
        """
        print(f"📂 Reading manual product data from {self.file_path}...")
        
        if not os.path.exists(self.file_path):
            print(f"❌ Error: {self.file_path} not found. Please create this file with the product JSON.")
            return None
            
        try:
            with open(self.file_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            print(f"❌ Error reading manual file: {e}")
            return None