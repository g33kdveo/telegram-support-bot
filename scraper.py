import os
import subprocess
# Force Playwright to look in the persistent directory for browsers
# This must be set before importing playwright or launching browsers
os.environ["PLAYWRIGHT_BROWSERS_PATH"] = "/app/pw-browsers"

from playwright.sync_api import sync_playwright
import time
import json
import re

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

    def get_products(self):
        print("🚀 Starting Playwright Browser...")
        
        # Self-healing: Install browser if missing (Runtime Fix)
        browser_path = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
        if not os.path.exists(browser_path) or not os.listdir(browser_path):
            print(f"⚠️ Browser directory {browser_path} missing. Installing Chromium...")
            try:
                subprocess.run(["playwright", "install", "chromium"], check=True)
                print("✅ Chromium installed successfully.")
            except Exception as e:
                print(f"❌ Failed to install Chromium: {e}")

        products = []
        
        with sync_playwright() as p:
            # Launch Chromium
            # args=['--no-sandbox'] is often needed in container environments
            browser = p.chromium.launch(headless=True, args=['--no-sandbox'])
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
            )
            page = context.new_page()

            # Setup API Interception
            captured_data = []
            def handle_response(response):
                # Capture anything that looks like a product list API
                if ("api/products" in response.url or "products.list" in response.url or "scrape" in response.url) and response.status == 200:
                    try:
                        print(f"📥 Captured API response: {response.url}")
                        json_body = response.json()
                        captured_data.append(json_body)
                    except: pass
            
            page.on("response", handle_response)

            try:
                # 1. Go to Login Page
                print("🔐 Navigating to login...")
                page.goto(f"{self.base_url}/login", timeout=60000)
                page.wait_for_load_state("domcontentloaded")
                
                # Give the page a moment to render overlays (Age Gate)
                time.sleep(1.5)

                # Check for Maintenance
                content = page.content()
                if "performing updates" in content or "Maintenance" in content or "Under Maintenance" in content:
                    print("⚠️ Site is Under Maintenance. Skipping browser scrape.")
                    raise Exception("Site Under Maintenance")

                # 2. Handle "21+" Age Gate
                try:
                    print("🔞 Checking for Age Gate...")
                    # Check any visible checkboxes (often required)
                    checkboxes = page.locator("input[type='checkbox']")
                    if checkboxes.count() > 0:
                        for i in range(checkboxes.count()):
                            if checkboxes.nth(i).is_visible():
                                checkboxes.nth(i).check()
                                time.sleep(0.2)
                    
                    # Click "Yes" / "Enter" / "Continue" buttons
                    # Use get_by_text with .last to find the specific button/label, not the container
                    clicked_age = False
                    possible_texts = [
                        re.compile(r"Yes, I Am 21\+", re.IGNORECASE),
                        re.compile(r"I Am 21\+", re.IGNORECASE),
                        re.compile(r"Yes, I am 21", re.IGNORECASE),
                        re.compile(r"Enter Site", re.IGNORECASE)
                    ]
                    
                    for p_text in possible_texts:
                        if clicked_age: break
                        # .last selects the deepest element (the text node/button) instead of the wrapper
                        btn = page.get_by_text(p_text).last
                        if btn.is_visible():
                            print(f"   Clicking Age Gate button: {btn.inner_text()}")
                            btn.click()
                            clicked_age = True
                            time.sleep(2)
                except Exception as e:
                    print(f"⚠️ Age gate interaction warning: {e}")

                # 3. Login
                if self.username and self.password:
                    print("⌨️ Filling credentials...")
                    # Wait for password field (most reliable indicator of login form)
                    try:
                        page.wait_for_selector("input[type='password']", timeout=5000)
                    except:
                        print("⚠️ Password field wait timeout - attempting to fill anyway...")

                    # Fill Username - Try multiple common selectors
                    user_filled = False
                    user_selectors = [
                        "input[name='username']", "input[name='email']", "input[type='email']",
                        "input[placeholder*='User']", "input[placeholder*='Email']"
                    ]
                    
                    for sel in user_selectors:
                        if page.locator(sel).first.is_visible():
                            page.fill(sel, self.username)
                            user_filled = True
                            break
                    
                    if not user_filled:
                        # Fallback to first text input
                        text_inputs = page.locator("input[type='text']")
                        if text_inputs.count() > 0 and text_inputs.first.is_visible():
                            text_inputs.first.fill(self.username)

                    # Fill Password
                    if page.locator("input[name='password']").is_visible():
                        page.fill("input[name='password']", self.password)
                    elif page.locator("input[type='password']").is_visible():
                        page.fill("input[type='password']", self.password)
                    
                    # Hit Enter to login
                    print("↵ Pressing Enter to login...")
                    page.keyboard.press("Enter")
                    
                    # Scan to see if shop opened (moved away from login)
                    login_success = False
                    try:
                        # Scan for up to 30s (increased for slow loading)
                        page.wait_for_url(lambda u: "/login" not in u, timeout=30000)
                        print("✅ Navigation detected (Login successful).")
                        login_success = True
                    except:
                        print("⚠️ Still on login page after Enter. Checking for obstacles...")
                        
                        # Check for Age Gate again
                        try:
                            possible_texts = [
                                re.compile(r"Yes, I Am 21\+", re.IGNORECASE),
                                re.compile(r"I Am 21\+", re.IGNORECASE),
                                re.compile(r"Yes, I am 21", re.IGNORECASE),
                                re.compile(r"Enter Site", re.IGNORECASE)
                            ]
                            for p_text in possible_texts:
                                btn = page.get_by_text(p_text).last
                                if btn.is_visible():
                                    print(f"   Found Age Gate again. Clicking: {btn.inner_text()}")
                                    btn.click()
                        except: pass

                        # Try clicking Login button explicitly
                        # Only if visible (avoid clicking if already transitioning)
                        login_btn = page.get_by_role("button", name=re.compile(r"Login|Sign In", re.IGNORECASE))
                        if login_btn.is_visible():
                            print("   Clicking Login button explicitly...")
                            try:
                                login_btn.click(timeout=5000)
                            except: pass
                            try:
                                page.wait_for_url(lambda u: "/login" not in u, timeout=10000)
                                print("✅ Navigation detected after click.")
                                login_success = True
                            except: pass
                        else:
                            print("ℹ️ Login button not visible. Assuming login in progress...")
                    
                    if login_success:
                        print("⏳ Shop opened. Waiting 2s for full load...")
                        time.sleep(2)
                    
                    if "/login" in page.url and not login_success:
                        print("⚠️ Warning: Still on login page. Login might have failed.")

                # 4. Navigate to Shop to trigger API
                print("📂 Loading Shop page to grab API...")
                page.goto(f"{self.base_url}/explore", timeout=60000)
                page.wait_for_load_state("networkidle", timeout=30000)
                
                # Wait a bit extra for any lazy-loaded APIs
                print("⏳ Waiting 2s for shop to fully open...")
                time.sleep(2)

                # 5. Find "Products API (JSON)" link at the bottom
                if not captured_data:
                    print("⚠️ No API data captured yet. Looking for 'Products API (JSON)' link...")
                    try:
                        # Scroll to bottom to ensure footer is loaded
                        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                        print("⬇️ Scrolled to bottom. Waiting for footer...")
                        try:
                            page.wait_for_selector("a[href='/api/products/scrape']", timeout=5000)
                        except:
                            time.sleep(2)
                        
                        # Find the link
                        # Try specific href first (more reliable), then text
                        api_link = page.locator("a[href='/api/products/scrape']").first
                        if not api_link.is_visible():
                            api_link = page.get_by_text("Products API (JSON)").first
                            
                        if api_link.is_visible():
                            print("✅ Found API link. Attempting to click...")
                            
                            current_url = page.url
                            clicked_success = False
                            
                            for i in range(5):
                                try:
                                    print(f"   Click attempt {i+1}...")
                                    api_link.click(timeout=2000)
                                    time.sleep(2)
                                    
                                    # Check if URL changed or body is JSON
                                    text = page.locator("body").inner_text()
                                    if page.url != current_url or text.strip().startswith("{") or text.strip().startswith("["):
                                        print("✅ Link opened successfully!")
                                        clicked_success = True
                                        break
                                    print("⚠️ No change detected. Retrying...")
                                except Exception as e:
                                    print(f"⚠️ Click attempt failed: {e}")
                                    time.sleep(1)
                            
                            if clicked_success:
                                page.wait_for_load_state("networkidle", timeout=10000)
                                text = page.locator("body").inner_text()
                                if text.strip().startswith("{") or text.strip().startswith("["):
                                    json_data = json.loads(text)
                                    print("📥 Captured JSON from API page body")
                                    captured_data.append(json_data)
                        else:
                            print("❌ 'Products API (JSON)' link not found after scrolling.")
                    except Exception as e:
                        print(f"⚠️ Failed to find/click API link: {e}")

            except Exception as e:
                print(f"⚠️ Browser interaction error: {e}")
                time.sleep(10) # Keep browser open for 10s so you can see the error

            browser.close()
            
            # Process captured data
            print(f"📊 Processing {len(captured_data)} captured API responses...")
            for data in captured_data:
                found = self.find_products_in_json(data)
                if found:
                    products.extend(found)
            
            # Deduplicate by name
            unique_products = {p['name']: p for p in products}.values()
            products = list(unique_products)

        if products:
            print(f"✅ Successfully found {len(products)} products via Playwright!")
            return {"data": products}
        else:
            print("❌ No products found via Playwright.")
            return {"data": [], "error": True}
        
        # Fallback to Manual File
        if os.path.exists("manual_products.json"):
            print("⚠️ Network scrape failed/Maintenance. Loading from manual_products.json...")
            try:
                with open("manual_products.json", "r", encoding="utf-8") as f:
                    raw_data = json.load(f)
                    found_products = self.find_products_in_json(raw_data)
                    if found_products:
                        print(f"✅ Loaded {len(found_products)} products from manual file")
                        return {"data": found_products}
            except Exception as e:
                print(f"❌ Failed to load manual file: {e}")

        print("❌ No products found via Playwright.")
        return {"data": [], "error": True}

# Test function
if __name__ == "__main__":
    username = os.getenv("CHADS_USERNAME")
    password = os.getenv("CHADS_PASSWORD")
    
    if username and password:
        scraper = ChadsFlooringScraper(username=username, password=password)
        result = scraper.get_products()
        print(f"Found {len(result.get('data', []))} products")
    else:
        print("Please set CHADS_USERNAME and CHADS_PASSWORD env vars to test.")