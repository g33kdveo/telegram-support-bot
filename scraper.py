import asyncio
import json
import os
import re
import hashlib
import time
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse
from pyppeteer import launch
import requests
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

_stored_cookies = []
_stored_user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

def get_stored_cookies():
    return _stored_cookies, _stored_user_agent


def fetch_authenticated_stock_snapshot(api_key, base_url="https://chadsflooring.bz"):
    cookies, user_agent = get_stored_cookies()
    if not cookies:
        return {"ok": False, "auth_required": True, "error": "No authenticated session"}

    cookie_jar = {
        cookie.get("name"): cookie.get("value")
        for cookie in cookies
        if cookie.get("name")
    }
    headers = {"User-Agent": user_agent}
    if api_key:
        headers["X-Api-Key"] = api_key

    try:
        response = requests.get(
            f"{base_url}/api/products/scrape",
            headers=headers,
            cookies=cookie_jar,
            timeout=30,
        )
        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After")
            try:
                retry_after = max(1.0, float(retry_after))
            except (TypeError, ValueError):
                try:
                    retry_at = parsedate_to_datetime(retry_after)
                    if retry_at.tzinfo is None:
                        retry_at = retry_at.replace(tzinfo=timezone.utc)
                    retry_after = max(1.0, (retry_at - datetime.now(timezone.utc)).total_seconds())
                except (TypeError, ValueError, OverflowError):
                    retry_after = 60.0
            return {
                "ok": False,
                "auth_required": False,
                "rate_limited": True,
                "retry_after": retry_after,
                "error": "HTTP 429: Too Many Requests",
            }
        if (
            response.status_code in (401, 403)
            or "login" in response.url.lower()
            or "text/html" in response.headers.get("Content-Type", "").lower()
        ):
            return {"ok": False, "auth_required": True, "error": f"HTTP {response.status_code}"}
        response.raise_for_status()
        payload = response.json()
    except Exception as e:
        return {"ok": False, "auth_required": False, "error": str(e)}

    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        return {"ok": False, "auth_required": False, "error": "Unexpected API response"}

    snapshot = {}
    for group in payload["data"]:
        if not isinstance(group, dict):
            continue
        group_id = str(group.get("id") or group.get("sku") or group.get("name"))
        snapshot[group_id] = {
            "name": str(group.get("name", group_id)),
            "category": str(group.get("cat") or group.get("category") or "Uncategorized"),
            "variants": {
                str(product.get("id") or product.get("sku") or product.get("name")): {
                    "name": str(product.get("name", "")),
                    "qty": product.get("qty"),
                }
                for product in group.get("products", [])
                if isinstance(product, dict)
            },
        }

    return {"ok": True, "auth_required": False, "snapshot": snapshot}

IMAGE_PATH_PREFIX = "/uploads/products/"
IMAGE_SIZE = 450
IMAGE_SIZE_VARIANTS = [950, 750, 450, 400, 375, 325, 300, 250, 225, 178, 80]
IMAGE_MIN_SIZE = 300  # Minimum acceptable image size to prevent tiny fallbacks
IMAGE_DOWNLOAD_BATCH_SIZE = 60
IMAGE_DOWNLOAD_CONCURRENCY = 12
IMAGE_FETCH_TIMEOUT = (3, 10)
IMAGE_ALL_FAIL_CHECK_COUNT = 75


def _apply_cached_image_references(groups, cached_images):
    for group in groups:
        group_images = group.get('imgs')
        if isinstance(group_images, dict):
            for image_key, image_value in group_images.items():
                if isinstance(image_value, str) and image_value in cached_images:
                    group_images[image_key] = f"__cached__:{cached_images[image_value]}"

        products = group.get('products')
        if not isinstance(products, list):
            continue
        for product in products:
            if not isinstance(product, dict):
                continue
            product_images = product.get('images')
            if isinstance(product_images, list):
                product['images'] = [
                    f"__cached__:{cached_images[image_value]}"
                    if isinstance(image_value, str) and image_value in cached_images
                    else image_value
                    for image_value in product_images
                ]


def _fetch_image_variants(variants, cookies, user_agent, referer):
    cookie_jar = requests.cookies.RequestsCookieJar()
    for cookie in cookies:
        name = cookie.get("name")
        if not name:
            continue
        cookie_jar.set(
            name,
            cookie.get("value", ""),
            domain=cookie.get("domain") or urlparse(referer).hostname,
            path=cookie.get("path") or "/",
        )

    headers = {
        "User-Agent": user_agent,
        "Referer": referer.rstrip("/") + "/",
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
    }
    failures = []
    for image_url in variants:
        try:
            response = requests.get(
                image_url,
                headers=headers,
                cookies=cookie_jar,
                timeout=IMAGE_FETCH_TIMEOUT,
            )
            content_type = response.headers.get("Content-Type", "").lower()
            if response.status_code == 429:
                return None, image_url, f"429 {image_url}", True
            if response.ok and content_type.startswith("image/") and len(response.content) > 500:
                return response.content, image_url, None, False
            failures.append(f"{response.status_code} {content_type or 'unknown content type'} {image_url}")
        except requests.RequestException as error:
            failures.append(f"{type(error).__name__} {image_url}")
    return None, None, failures[0] if failures else "no usable image variants", False

CATEGORIES = [
    "Accessories", "BYOB", "Carts", "Concentrates",
    "Disposables", "Edibles", "Flower", "Merch",
    "Munchies", "Pre-rolls", "Topical"
]


class RogersRoofingScraper:

    def __init__(self, username=None, password=None, cookie_string=None, api_key=None, login_callback=None):
        self.username = username
        self.password = password
        self.api_key = api_key
        self.login_callback = login_callback
        self.base_url = "https://chadsflooring.bz"
        self.rate_limit_retry_after = 0.0

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
            return False

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
            if self.login_callback:
                self.login_callback()
            return True
        else:
            print("WARNING: May still be on login page. Continuing anyway.")
            return False

    async def _fetch_scrape_endpoint(self, page):
        url = f"{self.base_url}/api/products/scrape"
        print(f"Fetching scrape endpoint: {url}")

        for attempt in range(3):
            try:
                json_text = await page.evaluate('''(url, apiKey) => {
                    const headers = {};
                    if (apiKey) {
                        headers['X-Api-Key'] = apiKey;
                    }
                    return fetch(url, {
                        credentials: 'include',
                        headers: headers
                    })
                        .then(r => {
                            if (r.status === 429) {
                                const retryHeader = r.headers.get('Retry-After');
                                let retryAfter = 60;
                                if (retryHeader) {
                                    const seconds = Number(retryHeader);
                                    if (Number.isFinite(seconds) && seconds > 0) {
                                        retryAfter = seconds;
                                    } else {
                                        const retryAt = Date.parse(retryHeader);
                                        if (Number.isFinite(retryAt)) {
                                            retryAfter = Math.max(1, (retryAt - Date.now()) / 1000);
                                        }
                                    }
                                }
                                return JSON.stringify({_error: 'HTTP 429', retry_after: retryAfter});
                            }
                            if (!r.ok) return JSON.stringify({_error: 'HTTP ' + r.status});
                            return r.text();
                        })
                        .catch(e => JSON.stringify({_error: e.message}));
                }''', url, self.api_key)

                if not json_text:
                    print(f"  Attempt {attempt+1}: Empty response")
                    await asyncio.sleep(3)
                    continue

                data = json.loads(json_text)
                if isinstance(data, dict) and data.get('_error'):
                    if data["_error"] == "HTTP 429":
                        try:
                            self.rate_limit_retry_after = max(1.0, float(data.get("retry_after") or 60))
                        except (TypeError, ValueError):
                            self.rate_limit_retry_after = 60.0
                        print(
                            f"  API rate limited; retry after "
                            f"{self.rate_limit_retry_after:.0f}s (not retrying immediately)"
                        )
                        return None
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

    def _resolve_image_url(self, img_val, img_prefix, size=None):
        """Resolve a single image URL, optionally with a specific size."""
        if not isinstance(img_val, str) or not img_val:
            return None

        resolved = img_val.strip()
        size_val = size if size is not None else IMAGE_SIZE

        if 'x_imgvariantsize' in resolved.lower():
            resolved = re.sub(r'x_imgvariantsize', f'x{size_val}', resolved, flags=re.IGNORECASE)
        elif re.search(r'(?i)(?:^|/)[xX]\d+-', resolved):
            pass
        elif resolved.startswith('/'):
            pass

        if resolved.startswith('http://') or resolved.startswith('https://'):
            return resolved
        elif resolved.startswith('/uploads/') or resolved.startswith('uploads/'):
            return self.base_url + '/' + resolved.lstrip('/')
        else:
            prefix = (img_prefix or IMAGE_PATH_PREFIX).strip('/')
            return f"{self.base_url.rstrip('/')}/{prefix}/{resolved.lstrip('/')}"
    
    def _get_image_url_variants(self, img_val, img_prefix):
        """Generate a list of image URL variants from largest to smallest, respecting minimum size."""
        if not isinstance(img_val, str) or not img_val:
            return []
        
        variants = []
        for size in IMAGE_SIZE_VARIANTS:
            if size >= IMAGE_MIN_SIZE:  # Only include sizes >= minimum
                url = self._resolve_image_url(img_val, img_prefix, size)
                if url:
                    variants.append(url)
        return list(dict.fromkeys(variants))

    async def _download_images(self, page, groups, img_prefix):
        img_cache_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cached_images")
        os.makedirs(img_cache_dir, exist_ok=True)

        print(f"\n--- DOWNLOADING IMAGES ---")

        # Build URL variant mapping (template -> list of variants from largest to smallest)
        url_variants_map = {}
        
        for g in groups:
            imgs = g.get('imgs')
            if isinstance(imgs, dict):
                for ikey, ival in imgs.items():
                    if isinstance(ival, str) and ival and ival not in url_variants_map:
                        variants = self._get_image_url_variants(ival, img_prefix)
                        if variants:
                            url_variants_map[ival] = variants

            for p in g.get('products', []):
                if not isinstance(p, dict):
                    continue
                for img_val in p.get('images', []):
                    if isinstance(img_val, str) and img_val and img_val not in url_variants_map:
                        variants = self._get_image_url_variants(img_val, img_prefix)
                        if variants:
                            url_variants_map[img_val] = variants

        # Build cache mapping using template key
        url_map = {}
        for img_template, variants in url_variants_map.items():
            if variants:
                ext = 'webp'
                if '.' in img_template:
                    raw_ext = img_template.rsplit('.', 1)[-1].lower()
                    if raw_ext in ('webp', 'png', 'jpg', 'jpeg', 'gif'):
                        ext = raw_ext
                # Use template as cache key so all sizes of same image use same cache file
                url_hash = hashlib.md5(img_template.encode()).hexdigest()[:16]
                cache_fname = f"{url_hash}.{ext}"
                url_map[img_template] = (variants, cache_fname, ext)

        print(f"  Unique images: {len(url_map)}")

        items = []
        cached_before = 0
        for img_template, cache_entry in url_map.items():
            cache_fname = cache_entry[1]
            cache_path = os.path.join(img_cache_dir, cache_fname)
            if os.path.isfile(cache_path) and os.path.getsize(cache_path) > 500:
                cached_before += 1
            else:
                items.append((img_template, cache_entry))
        print(f"  Reusing {cached_before} cached images; downloading {len(items)}")

        if items:
            cookies, user_agent = get_stored_cookies()
            downloaded = 0
            failed = 0
            total = len(items)
            start_time = time.time()
            rate_limited = False
            first_failure = None

            with ThreadPoolExecutor(max_workers=IMAGE_DOWNLOAD_CONCURRENCY) as executor:
                for i in range(0, total, IMAGE_DOWNLOAD_BATCH_SIZE):
                    batch = items[i:i + IMAGE_DOWNLOAD_BATCH_SIZE]
                    futures = {
                        executor.submit(
                            _fetch_image_variants,
                            variants,
                            cookies,
                            user_agent,
                            self.base_url,
                        ): (img_template, cache_fname)
                        for img_template, (variants, cache_fname, _) in batch
                    }

                    for future in as_completed(futures):
                        img_template, cache_fname = futures[future]
                        try:
                            image_bytes, used_url, error, is_rate_limited = future.result()
                        except Exception as error:
                            image_bytes, used_url, is_rate_limited = None, None, False
                            error = f"{type(error).__name__}: {error}"

                        if image_bytes:
                            with open(os.path.join(img_cache_dir, cache_fname), 'wb') as imgf:
                                imgf.write(image_bytes)
                            downloaded += 1
                        else:
                            failed += 1
                            if first_failure is None and error:
                                first_failure = error
                            rate_limited = rate_limited or is_rate_limited

                    processed = min(i + len(batch), total)
                    elapsed = time.time() - start_time
                    rate = processed / elapsed if elapsed > 0 else 0
                    remaining = (total - processed) / rate if rate > 0 else 0
                    print(
                        f"  Progress: {processed}/{total} ({downloaded} ok, {failed} fail) "
                        f"~{remaining:.0f}s remaining"
                    )
                    if first_failure:
                        print(f"  First image request failure: {first_failure}")
                    if rate_limited:
                        print("  Image source returned HTTP 429; stopping image requests for this scrape.")
                        break
                    if downloaded == 0 and processed >= IMAGE_ALL_FAIL_CHECK_COUNT:
                        print(
                            f"  No images succeeded in the first {processed}; "
                            "stopping image requests to avoid a catalog-wide retry storm."
                        )
                        break

            print(f"  Downloaded: {downloaded}, Failed: {failed}, Time: {time.time() - start_time:.1f}s")
        elif not url_map:
            print("  No images to download")
        else:
            print("  All detected images are already cached")

        cached_images = {}
        for img_template, (_, cache_fname, _) in url_map.items():
            cache_path = os.path.join(img_cache_dir, cache_fname)
            if os.path.isfile(cache_path) and os.path.getsize(cache_path) > 500:
                cached_images[img_template] = cache_fname

        _apply_cached_image_references(groups, cached_images)

        cached_count = len(os.listdir(img_cache_dir)) if os.path.exists(img_cache_dir) else 0
        print(f"  Total cached images on disk: {cached_count}")

        # Return url_map with primary URL for UI (will use highest available from cache)
        result_map = {}
        for img_template, (variants, cache_fname, ext) in url_map.items():
            if img_template in cached_images:
                result_map[img_template] = (variants[0], cache_fname)
        return result_map

    async def _scrape_async(self, download_images=True):
        print("=" * 60)
        print("Starting Rogers Roofing scraper (scrape endpoint)")
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

            login_successful = await self._login(page)

            scrape_data = await self._fetch_scrape_endpoint(page)
            if not scrape_data or not isinstance(scrape_data.get('data'), list):
                print("ERROR: Failed to fetch scrape endpoint")
                return {
                    "data": [],
                    "rate_limited": self.rate_limit_retry_after > 0,
                    "retry_after": self.rate_limit_retry_after,
                }

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

            if download_images:
                print("\n--- PRESERVING SOURCE IMAGE SIZES ---")
                print("  Concrete image sizes are kept as-is; only placeholder URLs get size variants.")
                try:
                    await self._download_images(page, all_groups, img_prefix)
                except Exception as e:
                    print(f"  Image download aborted safely: {e}")

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
                "login_successful": login_successful,
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

    def get_stock_snapshot(self):
        loop = None
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            return loop.run_until_complete(self._scrape_async(download_images=False))
        except Exception as e:
            print(f"Stock Snapshot Wrapper Error: {e}")
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
    scraper = RogersRoofingScraper(
        username=os.getenv("CHADS_USERNAME"),
        password=os.getenv("CHADS_PASSWORD")
    )
    result = scraper.get_products()
    count = len(result.get('data', []))
    print(f"Test Complete! Found {count} products.")
    with open("scraped_products_test.json", "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)
    print("Saved results to scraped_products_test.json")