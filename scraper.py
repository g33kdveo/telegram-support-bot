import asyncio
import json
import os
import hashlib
import base64
import time
import shutil
from pyppeteer import launch

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

_stored_cookies = []
_stored_user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

def get_stored_cookies():
    return _stored_cookies, _stored_user_agent

IMAGE_PATH_PREFIX = "/uploads/products/"
IMAGE_SIZE = 450
IMAGE_SIZE_VARIANTS = [950, 750, 450, 400, 375, 325, 300, 250, 225, 178, 80]

CATEGORIES = [
    "Accessories", "BYOB", "Carts", "Concentrates",
    "Disposables", "Edibles", "Flower", "Merch",
    "Munchies", "Pre-rolls", "Topical"
]


class ChadsFlooringScraper:

    def __init__(self, username=None, password=None, cookie_string=None):
        self.username = username
        self.password = password
        self.base_url = "https://chadsflooring.bz"

    def _find_chromium(self):
        exec_path = os.getenv("PUPPETEER_EXECUTABLE_PATH")
        if exec_path and os.path.exists(exec_path):
            return exec_path
        candidates = [
            shutil.which("chromium"),
            shutil.which("chromium-browser"),
            shutil.which("google-chrome"),
            "/usr/bin/chromium",
            "/usr/bin/chromium-browser",
            "/usr/bin/google-chrome",
        ]
        for p in candidates:
            if p and os.path.exists(p):
                return p
        nix_store = "/nix/store"
        if os.path.isdir(nix_store):
            for entry in os.listdir(nix_store):
                if "chromium" in entry:
                    candidate = os.path.join(nix_store, entry, "bin", "chromium")
                    if os.path.exists(candidate):
                        return candidate
        return None

    async def _handle_age_gate(self, page):
        try:
            page_content = await page.content()
            page_lower = page_content.lower()
            age_keywords = ['age', 'verify', '21', '18', 'old enough', 'legal age']
            if not any(kw in page_lower for kw in age_keywords):
                return False

            positive_words = ['yes', 'i am 21', 'i am over', 'i am of legal', 'agree', 'confirm', 'enter site']
            negative_words = ['no', 'under', 'not', 'exit', 'leave', 'cancel']

            for sel in ["button", "a.btn", "a.button", "input[type='button']", "input[type='submit']"]:
                elements = await page.querySelectorAll(sel)
                for el in elements:
                    text = await page.evaluate('(el) => (el.innerText || el.value || "").trim()', el)
                    if not text or len(text) > 80:
                        continue
                    text_lower = text.lower()
                    if any(neg in text_lower for neg in negative_words) and not any(pos in text_lower for pos in positive_words):
                        continue
                    if any(pos in text_lower for pos in positive_words):
                        try:
                            await page.evaluate('(el) => el.click()', el)
                        except:
                            try:
                                await el.click()
                            except:
                                continue
                        await asyncio.sleep(3)
                        return True

            for xpath in [
                "//button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'yes')]",
                "//a[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'yes')]",
            ]:
                try:
                    els = await page.xpath(xpath)
                    for el in els:
                        text = await page.evaluate('(el) => (el.innerText || "").trim()', el)
                        if any(neg in text.lower() for neg in negative_words):
                            continue
                        await page.evaluate('(el) => el.click()', el)
                        await asyncio.sleep(3)
                        return True
                except:
                    pass
            return False
        except Exception as e:
            print(f"Age gate error: {e}")
            return False

    async def _login(self, page):
        if not self.username or not self.password:
            print("No credentials provided, skipping login.")
            return

        print("Navigating to login page...")
        for attempt in range(3):
            try:
                await page.goto(f"{self.base_url}/login", {'waitUntil': 'domcontentloaded', 'timeout': 120000})
                break
            except Exception as e:
                print(f"  Login nav attempt {attempt+1} failed: {e}")
                if attempt == 2:
                    raise
                await asyncio.sleep(5)
        await asyncio.sleep(3)

        await self._handle_age_gate(page)

        try:
            await page.waitForSelector("input[type='password']", {'timeout': 10000})
        except:
            await asyncio.sleep(2)

        email_filled = False
        for sel in ["input[name='email']", "input[type='email']", "input[name='username']", "input[type='text']"]:
            try:
                el = await page.querySelector(sel)
                if el:
                    await page.evaluate('(el) => { el.value = ""; }', el)
                    await el.click()
                    await asyncio.sleep(0.3)
                    await page.type(sel, self.username, {'delay': 50})
                    email_filled = True
                    print(f"  Filled username using: {sel}")
                    break
            except:
                pass

        if not email_filled:
            print("WARNING: Could not find email/username input!")

        try:
            pwd_el = await page.querySelector("input[type='password']")
            if pwd_el:
                await pwd_el.click()
                await asyncio.sleep(0.3)
                await page.type("input[type='password']", self.password, {'delay': 50})
        except Exception as e:
            print(f"Password fill error: {e}")

        await asyncio.sleep(1)

        login_submitted = False
        for sel in ["button[type='submit']", "input[type='submit']", "button.login-btn", "button.btn-primary"]:
            try:
                btn = await page.querySelector(sel)
                if btn:
                    await btn.click()
                    login_submitted = True
                    break
            except:
                pass

        if not login_submitted:
            try:
                btns = await page.querySelectorAll("button")
                for btn in btns:
                    text = await page.evaluate('(el) => el.innerText || ""', btn)
                    if any(w in text.lower() for w in ['log in', 'login', 'sign in', 'submit']):
                        await btn.click()
                        login_submitted = True
                        break
            except:
                pass

        if not login_submitted:
            await page.keyboard.press('Enter')

        try:
            await page.waitForNavigation({'waitUntil': 'networkidle2', 'timeout': 30000})
        except:
            pass

        await asyncio.sleep(2)
        current_url = page.url
        if 'login' not in current_url.lower():
            print("Login successful.")
            global _stored_cookies, _stored_user_agent
            try:
                _stored_cookies = await page.cookies()
                ua = await page.evaluate('() => navigator.userAgent')
                if ua:
                    _stored_user_agent = ua
                print(f"  Stored {len(_stored_cookies)} cookies for image proxy")
            except Exception as ce:
                print(f"  Warning: Could not extract cookies: {ce}")
        else:
            print("WARNING: May still be on login page. Continuing anyway.")

    async def _fetch_scrape_endpoint(self, page):
        url = f"{self.base_url}/api/products/scrape"
        print(f"Fetching scrape endpoint: {url}")

        for attempt in range(3):
            try:
                json_text = await page.evaluate('''(url) => {
                    return fetch(url, {credentials: 'include'})
                        .then(r => {
                            if (!r.ok) return JSON.stringify({_error: 'HTTP ' + r.status});
                            return r.text();
                        })
                        .catch(e => JSON.stringify({_error: e.message}));
                }''', url)

                if not json_text:
                    print(f"  Attempt {attempt+1}: Empty response")
                    await asyncio.sleep(3)
                    continue

                data = json.loads(json_text)
                if isinstance(data, dict) and data.get('_error'):
                    print(f"  Attempt {attempt+1}: API error: {data['_error']}")
                    await asyncio.sleep(3)
                    continue

                if isinstance(data, dict) and isinstance(data.get('data'), list):
                    print(f"  Success: {len(data['data'])} product groups")
                    return data

                print(f"  Attempt {attempt+1}: Unexpected format, keys: {list(data.keys()) if isinstance(data, dict) else type(data)}")
                await asyncio.sleep(3)

            except Exception as e:
                print(f"  Attempt {attempt+1} error: {e}")
                await asyncio.sleep(3)

        return None

    def _resolve_image_url(self, img_val, img_prefix):
        if not isinstance(img_val, str) or not img_val:
            return None
        resolved = img_val.replace('x_imgvariantsize', f'x{IMAGE_SIZE}')
        if resolved.startswith('http://') or resolved.startswith('https://'):
            return resolved
        elif resolved.startswith('/uploads/') or resolved.startswith('uploads/'):
            return self.base_url + '/' + resolved.lstrip('/')
        else:
            return self.base_url + img_prefix + resolved

    async def _download_images(self, page, groups, img_prefix):
        img_cache_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cached_images")
        os.makedirs(img_cache_dir, exist_ok=True)
        print(f"\n--- DOWNLOADING IMAGES ---")

        url_map = {}
        for g in groups:
            imgs = g.get('imgs')
            if isinstance(imgs, dict):
                for ikey, ival in imgs.items():
                    if isinstance(ival, str) and ival:
                        img_url = self._resolve_image_url(ival, img_prefix)
                        if img_url:
                            url_hash = hashlib.md5(img_url.encode()).hexdigest()[:16]
                            ext = 'webp'
                            if '.' in ival:
                                raw_ext = ival.rsplit('.', 1)[-1].lower()
                                if raw_ext in ('webp', 'png', 'jpg', 'jpeg', 'gif'):
                                    ext = raw_ext
                            cache_fname = f"{url_hash}.{ext}"
                            url_map[ival] = (img_url, cache_fname)

            for p in g.get('products', []):
                if not isinstance(p, dict):
                    continue
                for img_val in p.get('images', []):
                    if isinstance(img_val, str) and img_val and img_val not in url_map:
                        img_url = self._resolve_image_url(img_val, img_prefix)
                        if img_url:
                            url_hash = hashlib.md5(img_url.encode()).hexdigest()[:16]
                            ext = 'webp'
                            if '.' in img_val:
                                raw_ext = img_val.rsplit('.', 1)[-1].lower()
                                if raw_ext in ('webp', 'png', 'jpg', 'jpeg', 'gif'):
                                    ext = raw_ext
                            cache_fname = f"{url_hash}.{ext}"
                            url_map[img_val] = (img_url, cache_fname)

        already_cached = set(os.listdir(img_cache_dir))
        to_download = {}
        for ival, (img_url, cache_fname) in url_map.items():
            if cache_fname not in already_cached:
                to_download[cache_fname] = img_url
        print(f"  Unique images: {len(url_map)}, already cached: {len(url_map) - len(to_download)}, to download: {len(to_download)}")

        if to_download and page:
            batch_size = 50
            items = list(to_download.items())
            downloaded = 0
            failed = 0
            total = len(items)
            start_time = time.time()
            for i in range(0, total, batch_size):
                batch = items[i:i + batch_size]
                urls_for_js = [url for _, url in batch]
                results = {}
                for attempt in range(2):
                    try:
                        results = await page.evaluate('''async (urls) => {
                            const out = {};
                            const CONCURRENCY = 10;
                            for (let i = 0; i < urls.length; i += CONCURRENCY) {
                                const chunk = urls.slice(i, i + CONCURRENCY);
                                await Promise.allSettled(chunk.map(async (url) => {
                                    try {
                                        const r = await fetch(url, {credentials: 'include'});
                                        if (!r.ok) { out[url] = {status: r.status}; return; }
                                        const blob = await r.blob();
                                        const reader = new FileReader();
                                        const b64 = await new Promise((resolve, reject) => {
                                            reader.onload = () => resolve(reader.result);
                                            reader.onerror = reject;
                                            reader.readAsDataURL(blob);
                                        });
                                        out[url] = {data: b64};
                                    } catch(e) { out[url] = {error: e.message}; }
                                }));
                            }
                            return out;
                        }''', urls_for_js)
                        break
                    except Exception as batch_err:
                        if attempt == 0:
                            print(f"  Batch retry after error: {batch_err}")
                            await asyncio.sleep(1)
                        else:
                            print(f"  Batch failed: {batch_err}")
                            results = {}

                for cache_fname, url in batch:
                    result = results.get(url, {})
                    b64_data = result.get('data') if isinstance(result, dict) else None
                    if b64_data and ',' in b64_data:
                        raw_bytes = base64.b64decode(b64_data.split(',', 1)[1])
                        if len(raw_bytes) > 500:
                            with open(os.path.join(img_cache_dir, cache_fname), 'wb') as imgf:
                                imgf.write(raw_bytes)
                            downloaded += 1
                            already_cached.add(cache_fname)
                        else:
                            failed += 1
                    else:
                        failed += 1
                processed = min(i + batch_size, total)
                elapsed = time.time() - start_time
                rate = processed / elapsed if elapsed > 0 else 0
                remaining = (total - processed) / rate if rate > 0 else 0
                print(f"  Progress: {processed}/{total} ({downloaded} ok, {failed} fail) ~{remaining:.0f}s remaining")
                await asyncio.sleep(0.05)
            print(f"  Downloaded: {downloaded}, Failed: {failed}, Time: {time.time() - start_time:.1f}s")
        else:
            print("  No new images to download")

        cached_count = len(already_cached)
        print(f"  Total cached images on disk: {cached_count}")

        return url_map

    async def _scrape_async(self):
        print("=" * 60)
        print("Starting Chads Flooring scraper (scrape endpoint)")
        print("=" * 60)
        browser = None

        try:
            exec_path = self._find_chromium()
            if not exec_path:
                print("ERROR: No chromium binary found.")
                return {"data": []}

            print(f"Using browser: {exec_path}")
            browser = await launch(
                headless=True,
                executablePath=exec_path,
                args=[
                    '--no-sandbox', '--disable-setuid-sandbox',
                    '--disable-dev-shm-usage', '--disable-gpu',
                    '--disable-extensions', '--no-first-run',
                    '--disable-background-networking', '--disable-default-apps',
                    '--disable-sync', '--disable-translate',
                    '--hide-scrollbars', '--mute-audio',
                ],
                autoClose=False,
                handleSIGINT=False, handleSIGTERM=False, handleSIGHUP=False,
                dumpio=False,
            )
            page = await browser.newPage()
            await page.setUserAgent(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            )
            await page.setViewport({'width': 1280, 'height': 800})

            print("Navigating to site...")
            for attempt in range(3):
                try:
                    await page.goto(self.base_url, {'waitUntil': 'domcontentloaded', 'timeout': 120000})
                    break
                except Exception as e:
                    print(f"  Nav attempt {attempt+1} failed: {e}")
                    if attempt == 2:
                        raise
                    await asyncio.sleep(5)
            await asyncio.sleep(3)

            content = await page.content()
            if "performing updates" in content.lower() or "maintenance" in content.lower():
                raise Exception("Site Under Maintenance")

            print("Checking age verification...")
            age_handled = await self._handle_age_gate(page)
            if age_handled:
                print("Age verification completed.")

            await self._login(page)

            scrape_data = await self._fetch_scrape_endpoint(page)
            if not scrape_data or not isinstance(scrape_data.get('data'), list):
                print("ERROR: Failed to fetch scrape endpoint")
                return {"data": []}

            all_groups = scrape_data['data']
            img_prefix = scrape_data.get('imagePathPrefix', IMAGE_PATH_PREFIX)
            if not img_prefix.startswith('/'):
                img_prefix = '/' + img_prefix
            if not img_prefix.endswith('/'):
                img_prefix += '/'

            total_variants = sum(len(g.get('products', [])) for g in all_groups)
            has_imgs = sum(1 for g in all_groups if g.get('imgs') and isinstance(g['imgs'], dict) and len(g['imgs']) > 0)
            total_stock = 0
            for g in all_groups:
                for p in g.get('products', []):
                    if isinstance(p, dict):
                        qty = p.get('qty', 0)
                        if isinstance(qty, (int, float)):
                            total_stock += int(qty)
                        elif isinstance(qty, str):
                            try:
                                total_stock += int(qty.replace('+', '').strip() or '0')
                            except:
                                pass

            cats_summary = {}
            for g in all_groups:
                cat = g.get('cat') or 'Uncategorized'
                cats_summary[cat] = cats_summary.get(cat, 0) + 1
            brands_summary = {}
            for g in all_groups:
                brand = g.get('brand') or 'Unknown'
                brands_summary[brand] = brands_summary.get(brand, 0) + 1

            print(f"\n{'='*60}")
            print(f"SCRAPE RESULTS:")
            print(f"  Groups: {len(all_groups)}")
            print(f"  Variants: {total_variants}")
            print(f"  Images: {has_imgs}/{len(all_groups)} groups have images")
            print(f"  Stock: {total_stock} total qty")
            print(f"\n  Categories:")
            for c, count in sorted(cats_summary.items(), key=lambda x: -x[1]):
                print(f"    {c}: {count}")
            print(f"\n  Top brands:")
            for b, count in sorted(brands_summary.items(), key=lambda x: -x[1])[:15]:
                print(f"    {b}: {count}")
            print(f"{'='*60}")

            await self._download_images(page, all_groups, img_prefix)

            try:
                await browser.close()
                browser = None
            except:
                pass

            return {
                "data": all_groups,
                "imagePathPrefix": img_prefix,
                "imageSizeVariants": scrape_data.get('imageSizeVariants', IMAGE_SIZE_VARIANTS),
                "lastUpdated": scrape_data.get('lastUpdated'),
                "nextUpdate": scrape_data.get('nextUpdate'),
            }

        except Exception as e:
            print(f"Scraper Error: {e}")
            import traceback
            traceback.print_exc()
        finally:
            if browser:
                try:
                    await browser.close()
                except:
                    pass

        return {"data": [], "imagePathPrefix": IMAGE_PATH_PREFIX}

    def get_products(self):
        loop = None
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            return loop.run_until_complete(self._scrape_async())
        except Exception as e:
            print(f"Scraper Wrapper Error: {e}")
            import traceback
            traceback.print_exc()
            return {"data": [], "error": True}
        finally:
            if loop:
                try:
                    loop.close()
                except:
                    pass


if __name__ == "__main__":
    print("Running local scraper test...")
    scraper = ChadsFlooringScraper(
        username=os.getenv("CHADS_USERNAME"),
        password=os.getenv("CHADS_PASSWORD")
    )
    result = scraper.get_products()
    count = len(result.get('data', []))
    print(f"Test Complete! Found {count} products.")
    with open("scraped_products_test.json", "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)
    print("Saved results to scraped_products_test.json")