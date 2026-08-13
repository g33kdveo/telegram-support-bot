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
                    
                    # Get parent metadata (Category, Brand)
                    parent_cat = group.get("cat")
                    parent_brand = group.get("brand")
                    
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
                            
                            # Ensure price is a float (number), not a string
                            try:
                                product["price"] = float(product.get("price", 0))
                            except:
                                product["price"] = 0.0
                            
                            # Inherit Category and Brand if missing in the variant
                            if parent_cat and not product.get("category"):
                                product["category"] = parent_cat
                                # Also set 'cat' key as some frontends might use it
                                product["cat"] = parent_cat
                            
                            # Default category if still missing (prevents items from being hidden)
                            if not product.get("category"):
                                product["category"] = "General"
                                product["cat"] = "General"
                            
                            if parent_brand and not product.get("brand"):
                                product["brand"] = parent_brand
                                
                            # ID Handling: Leave as is (usually int) to prevent frontend mismatch
                            # If missing, generate one to prevent crashes
                            if "id" not in product:
                                import uuid
                                product["id"] = str(uuid.uuid4())
                            
                            # Fallback for images: if variant has no images, use parent's
                            if "images" not in product or not product["images"]:
                                if parent_img:
                                    product["images"] = [parent_img]
                                else:
                                    product["images"] = []
                            
                            # Backward compatibility: Ensure 'image' field exists (first image)
                            if product.get("images") and len(product["images"]) > 0:
                                product["image"] = product["images"][0]
                            else:
                                product["image"] = ""
                            
                            # Extra Compatibility Fields (Just in case frontend uses these names)
                            product["img"] = product.get("image", "")
                            product["quantity"] = product.get("qty", 0)
                            product["stock"] = product.get("qty", 0)
                            product["inStock"] = product.get("qty", 0) > 0
                            product["status"] = "active"
                            product["isVisible"] = True
                            
                            flattened_products.append(product)
                
                print(f"✅ Flattened {len(flattened_products)} products from manual file.")
                if len(flattened_products) > 0:
                    print(f"🔍 Sample Product Data: {json.dumps(flattened_products[0], default=str)[:200]}...")
                # Return in the expected format, preserving metadata
                return {**raw_data, "data": flattened_products}
            
            return raw_data
        except Exception as e:
            print(f"❌ Error reading manual file: {e}")
            return None