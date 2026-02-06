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
                raw_data = json.load(f)
            
            # Check if it's the nested format (data -> list of groups -> products -> list of variants)
            # We need to flatten it so the API returns a list of actual products.
            if "data" in raw_data and isinstance(raw_data["data"], list):
                flattened_products = []
                
                for group in raw_data["data"]:
                    # Get parent image to use as fallback
                    parent_img = None
                    if "imgs" in group and isinstance(group["imgs"], dict):
                        # Use the first available image in the dict
                        for k, v in group["imgs"].items():
                            if v:
                                parent_img = v
                                break
                    
                    # Process variants
                    if "products" in group and isinstance(group["products"], list):
                        for variant in group["products"]:
                            # Create a copy to avoid modifying the original
                            product = variant.copy()
                            
                            # Fix Quantity: Convert strings like "500+" to integers (500)
                            # This ensures the frontend correctly sees them as > 0
                            raw_qty = product.get("qty", 0)
                            if isinstance(raw_qty, str):
                                try:
                                    digits = ''.join(filter(str.isdigit, raw_qty))
                                    product["qty"] = int(digits) if digits else 0
                                except:
                                    product["qty"] = 0
                            
                            # Fallback for images: if variant has no images, use parent's
                            if "images" not in product or not product["images"]:
                                if parent_img:
                                    product["images"] = [parent_img]
                                else:
                                    product["images"] = []
                            
                            flattened_products.append(product)
                
                print(f"✅ Flattened {len(flattened_products)} products from manual file.")
                # Return in the expected format, preserving metadata
                return {**raw_data, "data": flattened_products}
            
            return raw_data
        except Exception as e:
            print(f"❌ Error reading manual file: {e}")
            return None