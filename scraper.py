import asyncio
import json
import os
import time
from pyppeteer import launch

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

class ChadsFlooringScraper:
    def __init__(self, username=None, password=None, cookie_string=None):
        self.username = username
        self.password = password
        self.base_url = "https://chadsflooring.bz"

    def find_products_in_json(self, data):
        """Recursively search for product-like objects in JSON data"""
        products = []
        if isinstance(data, dict):
            if 'name' in data:
                name = data['name']
                if isinstance(name, str) and 2 < len(name) < 100 and ('id' in data or 'slug' in data or 'image' in data or 'description' in data):
                    p = {
                        'name': name,
                        'image': data.get('image', data.get('img', data.get('imageUrl', '')))
                    }
                    products.append(p)
            for value in data.values():
                products.extend(self.find_products_in_json(value))
        elif isinstance(data, list):
            for item in data:
                products.extend(self.find_products_in_json(item))
        return products

    async def _scrape_async(self):
        print("🚀 Starting Pyppeteer (Puppeteer)...")
        products = []
        browser = None
        try:
            # Launch Chromium with arguments to bypass container restrictions
            browser = await launch(
                headless=True,
                args=['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage'],
                autoClose=False
            )
            page = await browser.newPage()
            
            # Set a realistic User Agent
            await page.setUserAgent("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/110.0.0.0 Safari/537.36")
            await page.setViewport({'width': 1280, 'height': 800})

            captured_data = []

            # Setup Response Interception
            async def process_response(response):
                try:
                    if "api/products" in response.url or "products.list" in response.url or "scrape" in response.url:
                        if response.status == 200:
                            try:
                                json_body = await response.json()
                                print(f"📥 Captured API response: {response.url}")
                                captured_data.append(json_body)
                            except: pass
                except: pass
            
            page.on('response', lambda res: asyncio.ensure_future(process_response(res)))

            # 1. Navigate to Login
            print("🔐 Navigating to login...")
            await page.goto(f"{self.base_url}/login", {'waitUntil': 'networkidle2', 'timeout': 60000})
            await asyncio.sleep(2)

            # Check for Maintenance
            content = await page.content()
            if "performing updates" in content or "Maintenance" in content:
                print("⚠️ Site is Under Maintenance.")
                raise Exception("Site Under Maintenance")

            # 2. Handle Age Gate (Click Yes)
            print("🔞 Checking for Age Gate...")
            try:
                # XPath to find buttons containing "Yes" or "Enter"
                yes_buttons = await page.xpath("//button[contains(., 'Yes')] | //input[@type='button' and contains(@value, 'Yes')] | //button[contains(., 'Enter')]")
                if yes_buttons:
                    print("   Clicking 'Yes' button...")
                    await yes_buttons[0].click()
                    await asyncio.sleep(2)
                
                # Handle Checkboxes if any
                checkboxes = await page.querySelectorAll("input[type='checkbox']")
                for cb in checkboxes:
                    await cb.click()
            except Exception as e:
                print(f"⚠️ Age gate check warning: {e}")

            # 3. Login Process
            if self.username and self.password:
                print("⌨️ Filling credentials...")
                
                # Wait for password field to ensure form is loaded
                try:
                    await page.waitForSelector("input[type='password']", {'timeout': 5000})
                except: pass

                # Fill Username (Try 'email' first as requested, then fallback)
                email_input = await page.querySelector("input[name='email']")
                if email_input:
                    await page.type("input[name='email']", self.username)
                else:
                    await page.type("input[type='text']", self.username)

                # Fill Password
                await page.type("input[type='password']", self.password)
                
                print("↵ Pressing Enter to login...")
                await page.keyboard.press('Enter')
                
                # Wait for navigation
                try:
                    await page.waitForNavigation({'waitUntil': 'networkidle2', 'timeout': 30000})
                    print("✅ Login navigation detected.")
                except:
                    print("⚠️ Navigation timeout (might have loaded dynamically).")

            # 4. Navigate to Shop/Explore to trigger API
            print("📂 Loading Shop page...")
            await page.goto(f"{self.base_url}/explore", {'waitUntil': 'networkidle2', 'timeout': 60000})
            await asyncio.sleep(3)

            # 5. Fallback: Look for API Link if no data captured automatically
            if not captured_data:
                print("⚠️ No API data captured yet. Looking for 'Products API' link...")
                await page.evaluate('window.scrollTo(0, document.body.scrollHeight)')
                await asyncio.sleep(1)
                
                # Try to find link by text
                links = await page.xpath("//a[contains(., 'Products API') or contains(., 'JSON')]")
                if links:
                    print("✅ Found API link. Clicking...")
                    await links[0].click()
                    await asyncio.sleep(2)
                    
                    # Check if body contains JSON
                    body_text = await page.evaluate('document.body.innerText')
                    if body_text.strip().startswith('{') or body_text.strip().startswith('['):
                        try:
                            captured_data.append(json.loads(body_text))
                            print("📥 Captured JSON from page body.")
                        except: pass

            await browser.close()
            
            # Process Data
            print(f"📊 Processing {len(captured_data)} captured responses...")
            for data in captured_data:
                found = self.find_products_in_json(data)
                if found:
                    products.extend(found)
            
            # Deduplicate
            unique_products = {p['name']: p for p in products}.values()
            products = list(unique_products)

        except Exception as e:
            print(f"❌ Pyppeteer Error: {e}")
            if browser:
                await browser.close()
        
        return {"data": products}

    def get_products(self):
        """Synchronous wrapper for the async scraper to work with bot.py"""
        try:
            # Create a new event loop for this thread
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            return loop.run_until_complete(self._scrape_async())
        except Exception as e:
            print(f"❌ Scraper Wrapper Error: {e}")
            return {"data": [], "error": True}
        finally:
            try:
                loop.close()
            except: pass