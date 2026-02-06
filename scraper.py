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
                # Expanded criteria to catch more product variants
                if isinstance(name, str) and 2 < len(name) < 100:
                    # Try to find image in various keys
                    img = data.get('image', data.get('img', data.get('imageUrl', '')))
                    if not img and 'imgs' in data and isinstance(data['imgs'], dict):
                        # Handle {"imgs": {"b123": "filename.jpg"}} format
                        for v in data['imgs'].values():
                            if isinstance(v, str):
                                img = v
                                break

                    # Ensure price is a number
                    price = data.get('price', 0)
                    try:
                        price = float(price)
                    except:
                        price = 0

                    p = {
                        'name': name,
                        'image': img,
                        'price': price,
                        'id': data.get('id', '')
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
            # Smart executable path detection to avoid download errors
            exec_path = os.getenv("PUPPETEER_EXECUTABLE_PATH")
            if not exec_path:
                if os.name == 'nt': # Windows
                    possible_paths = [
                        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
                        os.path.expanduser(r"~\AppData\Local\Google\Chrome\Application\chrome.exe")
                    ]
                    for p in possible_paths:
                        if os.path.exists(p):
                            exec_path = p
                            break
                else: # Linux/Railway
                    possible_paths = ["/usr/bin/chromium", "/usr/bin/chromium-browser", "/usr/bin/google-chrome"]
                    for p in possible_paths:
                        if os.path.exists(p):
                            exec_path = p
                            break

            launch_kwargs = {
                'headless': False if os.name == 'nt' else True, # Visible on Windows, Headless on Linux
                'args': ['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage', '--disable-gpu'],
                'autoClose': False
            }

            if exec_path:
                print(f"ℹ️ Using detected browser: {exec_path}")
                launch_kwargs['executablePath'] = exec_path

            # Launch Chromium
            browser = await launch(**launch_kwargs)
            page = await browser.newPage()
            
            # Set a realistic User Agent
            await page.setUserAgent("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/110.0.0.0 Safari/537.36")
            await page.setViewport({'width': 1280, 'height': 800})

            captured_data = []
            background_tasks = set()

            # Setup Response Interception
            async def process_response(response):
                try:
                    # Filter for JSON responses
                    if response.request.resourceType in ['xhr', 'fetch', 'document']:
                        if "api" in response.url or "json" in response.url or "products" in response.url:
                            try:
                                json_body = await response.json()
                                if isinstance(json_body, (dict, list)):
                                    captured_data.append(json_body)
                            except: pass
                except: pass
            
            def handle_response(res):
                task = asyncio.create_task(process_response(res))
                background_tasks.add(task)
                task.add_done_callback(background_tasks.discard)

            page.on('response', handle_response)

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

            # 5. Look for 'Products API' link (Always check to ensure full catalog)
            print("🔎 Looking for 'Products API' link...")
            try:
                await page.evaluate('window.scrollTo(0, document.body.scrollHeight)')
                await asyncio.sleep(1)
                
                # Try to find link by text - Prioritize "JSON" to avoid HTML documentation links
                links = await page.xpath("//a[contains(., 'JSON')]")
                
                if not links:
                    links = await page.xpath("//a[contains(., 'Products API')]")

                if links:
                    target_link = links[0]
                    link_text = await page.evaluate('(el) => el.innerText', target_link)
                    print(f"✅ Found link: '{link_text}'. Clicking...")
                    
                    # Scroll into view to ensure clickability
                    await page.evaluate('(el) => el.scrollIntoView()', target_link)
                    await asyncio.sleep(1)
                    
                    # Click using JS
                    try:
                        await page.evaluate('(el) => el.click()', target_link)
                    except: pass
                    
                    print("⏳ Waiting for URL change/load...")
                    try:
                        await page.waitForNavigation({'waitUntil': 'networkidle2', 'timeout': 20000})
                        print("✅ Navigation detected.")
                    except:
                        print("⚠️ No navigation detected (might be new tab or same page).")

                    print(f"📄 Current URL: {page.url}")
                    await asyncio.sleep(15) # Wait for JSON to render
                    
                    # Check if a new tab opened
                    pages = await browser.pages()
                    if len(pages) > 1:
                        print("📑 New tab detected, switching...")
                        page = pages[-1]
                        await page.bringToFront()
                    
                    # Check if body contains JSON
                    body_text = await page.evaluate('document.body.innerText')
                    if body_text.strip().startswith('{') or body_text.strip().startswith('['):
                        try:
                            captured_data.append(json.loads(body_text))
                            print("📥 Captured JSON from page body.")
                        except: 
                            print(f"⚠️ Failed to parse body JSON. Start: {body_text[:50]}...")
            except Exception as e:
                print(f"⚠️ API Link interaction failed: {e}")

            # Cleanup tasks
            for task in background_tasks:
                task.cancel()
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

if __name__ == "__main__":
    print("🕷️ Running local scraper test...")
    scraper = ChadsFlooringScraper(username=os.getenv("CHADS_USERNAME"), password=os.getenv("CHADS_PASSWORD"))
    result = scraper.get_products()
    count = len(result.get('data', []))
    print(f"✅ Test Complete! Found {count} products.")
    
    # Save to file for inspection
    with open("scraped_products_test.json", "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)
    print("📁 Saved results to scraped_products_test.json (Check this file to verify images/prices)")