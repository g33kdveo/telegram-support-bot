# GeekdHouse Telegram Support Bot

## Overview
A Telegram bot for GeekdHouse that handles customer support tickets, product browsing via a WebApp, referral system, and product scraping from Chadsflooring.bz. The bot runs with a built-in HTTP server that serves the webapp and product API.

## Architecture
- **bot.py** - Main bot application (~2500 lines). Handles Telegram commands, ticket management, admin features, referral system, and runs an HTTP server in a background thread.
- **scraper.py** - Pyppeteer-based scraper (~370 lines) that logs into Chadsflooring.bz and fetches the entire product catalog from `/api/products/scrape` endpoint in a single request.
- **webapp.html** - Telegram WebApp frontend for browsing products, served by the built-in HTTP server.

## Key Configuration
- **Port 5000**: The HTTP server serves `webapp.html` and `/api/products` endpoint
- **SQLite**: Uses `bot_database.db` for tickets, referrals, reviews, etc.
- **Chromium**: Required for pyppeteer scraper (installed via Nix)

## Environment Variables (Replit Secrets)
- `BOT_TOKEN` - Telegram bot token
- `ADMIN_IDS` - Comma-separated Telegram user IDs for admin access
- `SUPPORT_GROUP_ID` - Telegram group ID for support tickets
- `CHADS_USERNAME` - Login username for Chadsflooring.bz
- `CHADS_PASSWORD` - Login password for Chadsflooring.bz
- `PORT` - HTTP server port (set to 5000)
- `WEBAPP_URL` - Full URL to webapp.html
- `PUPPETEER_EXECUTABLE_PATH` - Path to Chromium binary

## How It Works
1. Bot starts and initializes the SQLite database
2. HTTP server starts on PORT 5000 in a background thread
3. Bot polls Telegram for updates
4. First scrape runs 30s after boot, then auto-refreshes every 6 hours
5. Fresh scrape = complete catalog from `/api/products/scrape`
6. Users interact via Telegram commands and the WebApp menu

## Scraper Architecture (scraper.py)
The scraper uses the dedicated `/api/products/scrape` endpoint:

1. **Login**: Launch Chromium, age verification click, email/password login
2. **Fetch**: Single authenticated request to `/api/products/scrape` returns entire catalog
3. **Image Download**: Download all product images using authenticated browser session
4. **Return**: Complete catalog with cached image references

### Scrape Endpoint Response Format
```json
{
  "lastUpdated": 1770766971703,
  "nextUpdate": 1770767331703,
  "imagePathPrefix": "/uploads/products/",
  "imageSizeVariants": [950, 750, 450, 400, 375, 325, 300, 250, 225, 178, 80],
  "data": [
    {
      "name": "Product Name",
      "desc": "Description",
      "brand": "Brand Name" | null,
      "cat": "Category" | null,
      "tags": [],
      "imgs": {"b1234": "x250-filename.jpg.webp"},
      "products": [
        {
          "name": "Variant Name",
          "id": 1234,
          "price": 29.99,
          "qty": 10 | "500+",
          "desc": "Description",
          "tags": [],
          "images": ["x_imgvariantsize-filename.jpg.webp"],
          "tiers": [{"price": 15.99, "qty": "5+"}]
        }
      ]
    }
  ]
}
```

### Image URL Format
- Group-level `imgs` values have size baked in: `x250-filename.jpg.webp`
- Product-level `images` use placeholder: `x_imgvariantsize-filename.jpg.webp`
- Replace `x_imgvariantsize` with `x{size}` (e.g., `x450`) for actual URL
- Full URL: `https://chadsflooring.bz/uploads/products/x450-filename.jpg.webp`

### Hardcoded Categories (from API)
Accessories, BYOB, Carts, Concentrates, Disposables, Edibles, Flower, Merch, Munchies, Pre-rolls, Topical (+ some null/uncategorized)

### Data Stats (typical)
- ~1741 product groups
- ~14053 variants
- ~371 unique brands (In-House is largest with ~232 groups)
- ~28880 total stock qty
- All variants have prices
- ~7200 variants have tier pricing

## Cache Strategy
- Fresh scrape directly replaces PRODUCT_CACHE
- Saved to `scraped_products.json` for persistence across restarts
- On boot, loads from `scraped_products.json` if available
- Failed scrapes keep existing cache intact

## Image Serving (Transparent Proxy + Cache)
- chadsflooring.bz images are PUBLIC (no authentication needed)
- Product data keeps ORIGINAL image paths (no `__cached__:` replacement) for reliability
- All images served via `/api/img?u=<path>` proxy endpoint
- Proxy flow: compute MD5 hash of full URL → check `cached_images/{hash}.{ext}` → serve from cache if exists → otherwise fetch from chadsflooring.bz → save to cache for next time
- Image URL format: `https://chadsflooring.bz/uploads/products/x{SIZE}-{filename}.png.webp`
- Cache is self-filling: proxy saves fetched images to cache automatically
- Legacy `__cached__:` format still supported for backward compatibility

## WebApp Display Rules
- **In-House brand** items display as brand "Value"
- In-House Carts/Disposables/Edibles stay in their original category tabs
- In-House Flower/Concentrates/Pre-rolls/etc go to "In-House" category tab
- Merch and Munchies categories are hidden
- Null/uncategorized items go to "Other" category
- Category order: Flower, Concentrates, Carts, Disposables, Pre-rolls, Edibles, In-House, BYOB, Accessories, Topical, Other
- Cards show: brand, name, formulation, options count, image, stock count, volume (NO prices, reviews, or stars)
- Modal shows: description, all variant options with name and stock (NO prices, tiers, reviews, or stars)
- Qty can be number or string (e.g., "500+") - displayed as-is for strings
- API data is processed exactly as received - 10/10 sync fidelity, no modifications to scraped data
- All hardcoded category edits, In-House routing, and display rules are preserved as-is

## Recent Changes
- 2026-02-11: Fixed overlapping scrape issue: SCRAPE_IN_PROGRESS flag with thread lock prevents duplicate login/scrape cycles
- 2026-02-11: Faster image downloads: batch size 15→50, 10-concurrent fetches per batch, 0.2s→0.05s inter-batch delay, progress/ETA logging
- 2026-02-10: Removed all price, tier, review, and star displays from webapp (cards and modal)
- 2026-02-10: Descriptions kept in modal view
- 2026-02-10: MAJOR REWRITE - Scraper now uses `/api/products/scrape` endpoint (single request for entire catalog, ~370 lines vs 1150)
- 2026-02-10: Removed all complex category/brand/query fetching, network interception, __NEXT_DATA__ parsing
- 2026-02-10: Modal variant display as list with name/stock instead of dropdown
- 2026-02-10: Null categories mapped to "Other"
- 2026-02-11: MAJOR IMAGE FIX - Transparent proxy+cache: keep original image paths in data, proxy checks cache by URL hash, fetches from source with cookies if not cached, saves to cache for next time
- 2026-02-10: Image proxy endpoint `/api/img?u=<path>` for serving images
- 2026-02-10: In-House brand routing: Carts/Disposables/Edibles stay in original categories (brand "Value"), Flower/Concentrates/etc go to "In-House" tab
