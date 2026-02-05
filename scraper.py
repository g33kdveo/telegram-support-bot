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
            
        # Potential login pages to check
        login_pages = [
            "/login?callbackUrl=https%3A%2F%2Fchadsflooring.bz",
            "/login", 
            "/signin", 
            "/auth/login", 
            "/user/login"
        ]
        login_page = ""
        login_page_url = f"{self.base_url}/login" # Default
        
        print("🔐 Detecting login page...")
        
        try:
            # ATTEMPT 0: NextAuth.js (Common with callbackUrl)
            # Try to fetch CSRF from API first, as this is cleaner than scraping HTML for Next.js
            try:
                csrf_url = f"{self.base_url}/api/auth/csrf"
                req = urllib.request.Request(csrf_url, headers={'User-Agent': 'Mozilla/5.0'})
                with self.opener.open(req) as response:
                    data = json.loads(response.read().decode('utf-8'))
                    csrf_token = data.get('csrfToken')
                    
                if csrf_token:
                    print(f"✅ Found NextAuth CSRF token: {csrf_token[:10]}...")
                    # Perform NextAuth Login
                    login_url = f"{self.base_url}/api/auth/callback/credentials"
                    payload = {
                        'redirect': 'false',
                        'csrfToken': csrf_token,
                        'username': self.username,
                        'password': self.password,
                        'callbackUrl': self.base_url
                    }
                    data_encoded = urllib.parse.urlencode(payload).encode('utf-8')
                    req = urllib.request.Request(
                        login_url, 
                        data=data_encoded,
                        headers={
                            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                            'Content-Type': 'application/x-www-form-urlencoded'
                        }
                    )
                    with self.opener.open(req) as response:
                        print("✅ NextAuth Login successful")
                        return True
            except Exception as e:
                print(f"⚠️ NextAuth attempt failed: {e}")

            for path in login_pages:
                try:
                    url = f"{self.base_url}{path}"
                    req = urllib.request.Request(
                        url,
                        headers={
                            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                        }
                    )
                    with self.opener.open(req) as response:
                        login_page = response.read().decode('utf-8')
                        login_page_url = url
                        print(f"✅ Found login page at: {path}")
                        break
                except Exception:
                    continue
            
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
            
            # If username looks like email, add email field too
            if '@' in self.username:
                login_data['email'] = self.username
            
            # Add CSRF token if found
            if csrf_token:
                login_data['csrf_token'] = csrf_token
                login_data['_token'] = csrf_token
                login_data['authenticity_token'] = csrf_token
            
            # Determine POST URL from form action or defaults
            login_url = f"{self.base_url}/api/auth/login" # Default
            
            if login_page:
                action_match = re.search(r'<form[^>]+action=["\']([^"\']+)["\']', login_page, re.IGNORECASE)
                if action_match:
                    action = action_match.group(1)
                    if not action.startswith('http'):
                        if action.startswith('/'):
                            login_url = f"{self.base_url}{action}"
                        else:
                            login_url = f"{self.base_url}/{action}"
                    else:
                        login_url = action
                    print(f"✅ Detected login action URL: {login_url}")
            
            # ATTEMPT 1: JSON Login (Most likely for /api/ endpoints)
            try:
                json_data = json.dumps(login_data).encode('utf-8')
                headers = {
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                    'Content-Type': 'application/json',
                    'Accept': 'application/json, text/plain, */*',
                    'Origin': self.base_url,
                    'Referer': login_page_url,
                }
                # Add XSRF-TOKEN from cookies if present
                for cookie in self.cookie_jar:
                    if cookie.name == 'XSRF-TOKEN':
                        headers['X-XSRF-TOKEN'] = urllib.parse.unquote(cookie.value)
                        break
                
                req = urllib.request.Request(login_url, data=json_data, headers=headers)
                with self.opener.open(req) as response:
                    print(f"✅ Login successful (JSON)")
                    return True
            except Exception as e:
                print(f"⚠️ JSON Login failed: {e}, trying Form Data...")

            # ATTEMPT 2: Form Data Login (Fallback)
            login_data_encoded = urllib.parse.urlencode(login_data).encode('utf-8')
            req = urllib.request.Request(
                login_url,
                data=login_data_encoded,
                headers={
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                    'Content-Type': 'application/x-www-form-urlencoded',
                    'Accept': 'application/json, text/plain, */*',
                    'Origin': self.base_url,
                    'Referer': login_page_url,
                }
            )
            
            with self.opener.open(req) as response:
                result = response.read().decode('utf-8')
                print(f"✅ Login successful (Form)")
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
                "/products/api/all",
                "/api/v1/products",
                "/api/products/all"
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