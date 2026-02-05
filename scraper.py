import json
import os
import urllib.request
import urllib.parse
import http.cookiejar
from html.parser import HTMLParser
import re
import time

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

class ChadsFlooringScraper:
    def __init__(self, username=None, password=None):
        self.username = username
        self.password = password
        self.base_url = "https://chadsflooring.bz"
        
        # Setup cookie handling for session management
        self.cookie_jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.cookie_jar)
        )
        
    def login(self):
        """Login to chadsflooring.bz and get session cookies"""
        if not self.username or not self.password:
            return False
            
        try:
            # First, get the login page to get any CSRF tokens
            login_page_url = f"{self.base_url}/login"
            req = urllib.request.Request(
                login_page_url,
                headers={
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                }
            )
            
            with self.opener.open(req) as response:
                login_page = response.read().decode('utf-8')
            
            # Try to extract any tokens from the page
            # Look for common patterns like csrf_token, authenticity_token, etc.
            token_patterns = [
                r'name="csrf_token"\s+value="([^"]+)"',
                r'name="_token"\s+value="([^"]+)"',
                r'name="authenticity_token"\s+value="([^"]+)"',
                r'"csrfToken":"([^"]+)"',
                r'window\.csrfToken\s*=\s*"([^"]+)"'
            ]
            
            csrf_token = None
            for pattern in token_patterns:
                match = re.search(pattern, login_page)
                if match:
                    csrf_token = match.group(1)
                    break
            
            # Prepare login data
            login_data = {
                'username': self.username,
                'password': self.password,
            }
            
            # Add CSRF token if found
            if csrf_token:
                login_data['csrf_token'] = csrf_token
                # Also try common variations
                login_data['_token'] = csrf_token
                login_data['authenticity_token'] = csrf_token
            
            # Try to login
            login_url = f"{self.base_url}/api/auth/login"
            login_data_encoded = urllib.parse.urlencode(login_data).encode('utf-8')
            
            req = urllib.request.Request(
                login_url,
                data=login_data_encoded,
                headers={
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                    'Content-Type': 'application/x-www-form-urlencoded',
                    'Accept': 'application/json, text/plain, */*',
                    'Origin': self.base_url,
                    'Referer': f"{self.base_url}/login",
                }
            )
            
            with self.opener.open(req) as response:
                result = response.read().decode('utf-8')
                print(f"✅ Login successful")
                return True
                
        except Exception as e:
            print(f"❌ Login failed: {str(e)}")
            return False
    
    def fetch_products_api(self):
        """Try to fetch products using the API with authentication"""
        try:
            # Try different API endpoints
            api_endpoints = [
                "/api/products/scrape",
                "/api/products",
                "/api/inventory",
                "/products/api/all"
            ]
            
            for endpoint in api_endpoints:
                try:
                    url = f"{self.base_url}{endpoint}"
                    req = urllib.request.Request(
                        url,
                        headers={
                            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                            'Accept': 'application/json, text/plain, */*',
                            'Referer': self.base_url,
                        }
                    )
                    
                    with self.opener.open(req, timeout=10) as response:
                        data = response.read().decode('utf-8')
                        
                        # Check if it's JSON
                        try:
                            json_data = json.loads(data)
                            print(f"✅ Successfully fetched from API: {endpoint}")
                            return json_data
                        except json.JSONDecodeError:
                            continue
                            
                except Exception as e:
                    continue
                    
            return None
            
        except Exception as e:
            print(f"❌ API fetch failed: {str(e)}")
            return None
    
    def scrape_products_html(self):
        """Scrape products directly from HTML pages"""
        try:
            products = []
            
            # Common product listing URLs to try
            product_urls = [
                "/products",
                "/shop",
                "/menu",
                "/inventory",
                "/store"
            ]
            
            for product_path in product_urls:
                try:
                    url = f"{self.base_url}{product_path}"
                    req = urllib.request.Request(
                        url,
                        headers={
                            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                        }
                    )
                    
                    with self.opener.open(req, timeout=10) as response:
                        html = response.read().decode('utf-8')
                        
                        # Parse products from HTML
                        # Look for common patterns in product listings
                        product_patterns = [
                            # JSON-LD structured data
                            r'<script type="application/ld\+json">(.*?)</script>',
                            # Data attributes
                            r'data-product=\'({.*?})\'',
                            r'data-product="({.*?})"',
                            # JavaScript variables
                            r'var products\s*=\s*(\[.*?\]);',
                            r'window\.products\s*=\s*(\[.*?\]);',
                        ]
                        
                        for pattern in product_patterns:
                            matches = re.findall(pattern, html, re.DOTALL)
                            for match in matches:
                                try:
                                    # Try to parse as JSON
                                    data = json.loads(match)
                                    if isinstance(data, list):
                                        products.extend(data)
                                    elif isinstance(data, dict):
                                        products.append(data)
                                except:
                                    continue
                        
                        # If we found products, return them
                        if products:
                            print(f"✅ Found {len(products)} products from {product_path}")
                            return {"data": products}
                            
                except Exception as e:
                    continue
                    
            # If no structured data found, try to parse HTML elements
            if not products:
                products = self.parse_product_cards(html)
                if products:
                    return {"data": products}
                    
            return None
            
        except Exception as e:
            print(f"❌ HTML scraping failed: {str(e)}")
            return None
    
    def parse_product_cards(self, html):
        """Parse product information from HTML cards/divs"""
        products = []
        
        # Common product card patterns
        product_card_patterns = [
            r'<div[^>]*class="[^"]*product[^"]*"[^>]*>(.*?)</div>',
            r'<article[^>]*class="[^"]*product[^"]*"[^>]*>(.*?)</article>',
            r'<li[^>]*class="[^"]*product[^"]*"[^>]*>(.*?)</li>',
        ]
        
        # Extract product info patterns
        name_pattern = r'(?:title|name)="([^"]+)"|>([^<]+)</(?:h\d|a|span)>'
        price_pattern = r'\$?([\d,]+\.?\d*)'
        image_pattern = r'(?:src|data-src)="([^"]+)"'
        
        for card_pattern in product_card_patterns:
            cards = re.findall(card_pattern, html, re.DOTALL)
            
            for card in cards:
                product = {}
                
                # Extract name
                name_match = re.search(name_pattern, card)
                if name_match:
                    product['name'] = name_match.group(1) or name_match.group(2)
                
                # Extract price
                price_match = re.search(price_pattern, card)
                if price_match:
                    product['price'] = price_match.group(1).replace(',', '')
                
                # Extract image
                image_match = re.search(image_pattern, card)
                if image_match:
                    product['image'] = image_match.group(1)
                    if not product['image'].startswith('http'):
                        product['image'] = self.base_url + product['image']
                
                if product.get('name'):
                    products.append(product)
        
        return products
    
    def get_products(self):
        """Main method to get products using all available methods"""
        
        # Try to login if credentials provided
        if self.username and self.password:
            print("🔐 Attempting login...")
            self.login()
        
        # Try API first
        print("🔍 Trying API endpoints...")
        data = self.fetch_products_api()
        if data:
            return data
        
        # Fallback to HTML scraping
        print("🔍 Trying HTML scraping...")
        data = self.scrape_products_html()
        if data:
            return data
        
        # Return empty if all methods fail
        print("⚠️ All methods failed")
        return {
            "data": [],
            "error": "Unable to fetch products",
            "message": "Could not retrieve product data from chadsflooring.bz"
        }

# Test function
if __name__ == "__main__":
    # Test without credentials
    scraper = ChadsFlooringScraper()
    result = scraper.get_products()
    print(f"Found {len(result.get('data', []))} products")
    
    # To test with credentials:
    username = os.getenv("CHADS_USERNAME")
    password = os.getenv("CHADS_PASSWORD")
    
    if username and password:
        scraper = ChadsFlooringScraper(username=username, password=password)
        result = scraper.get_products()