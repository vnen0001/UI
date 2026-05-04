#!/usr/bin/env python3
"""
Shopee.vn Product Scraper  (patchright edition)
================================================
Uses patchright — a patched Playwright that removes the low-level CDP signals
that Shopee's TrustDecision anti-bot SDK specifically looks for.
Also uses a persistent Chrome profile so the browser has real-looking history/cache.

Step 1 – Login:   python scraper.py --login
Step 2 – Scrape:  python scraper.py --url "https://shopee.vn/search?keyword=laptop" --pages 10
"""

import asyncio
import json
import csv
import re
import random
import argparse
from pathlib import Path
from datetime import datetime
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

from patchright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

# ─────────────────────────────────────────────
# Paths
# ─────────────────────────────────────────────
BASE_DIR     = Path(__file__).parent
PROFILE_DIR  = BASE_DIR / "chrome_profile"   # persistent user-data-dir
PROFILE_DIR.mkdir(exist_ok=True)

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
VIEWPORT = {"width": 1440, "height": 900}


# ─────────────────────────────────────────────
# Browser helpers
# ─────────────────────────────────────────────
async def human_delay(min_ms: int = 900, max_ms: int = 3000):
    await asyncio.sleep(random.uniform(min_ms, max_ms) / 1000)


async def scroll_naturally(page, rounds: int = 7):
    for _ in range(rounds):
        await page.evaluate(
            f"window.scrollBy({{ top: {random.randint(200, 650)}, behavior: 'smooth' }})"
        )
        await asyncio.sleep(random.uniform(0.35, 0.9))
    await asyncio.sleep(0.4)
    await page.evaluate("window.scrollTo(0, 0)")
    await asyncio.sleep(0.3)


def launch_args():
    return [
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--disable-infobars",
        "--disable-notifications",
        "--start-maximized",
        # Do NOT include --disable-blink-features=AutomationControlled —
        # patchright patches this at a lower level; adding it manually can
        # actually create a detectable fingerprint anomaly.
    ]


async def open_persistent_browser(playwright, *, headless: bool = False):
    """
    Launch Chromium with a persistent user-data-dir.
    patchright patches the CDP signals internally; no extra stealth JS needed.
    """
    context = await playwright.chromium.launch_persistent_context(
        str(PROFILE_DIR),
        headless=headless,
        args=launch_args(),
        viewport=VIEWPORT,
        user_agent=USER_AGENT,
        locale="vi-VN",
        timezone_id="Asia/Ho_Chi_Minh",
        extra_http_headers={
            "Accept-Language": "vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7",
        },
        ignore_default_args=["--enable-automation"],   # hides automation banner
    )
    return context


# ─────────────────────────────────────────────
# Pagination URL builder
# ─────────────────────────────────────────────
def build_page_url(base_url: str, page_num: int) -> str:
    parsed = urlparse(base_url)
    params = parse_qs(parsed.query, keep_blank_values=True)
    params.pop("page", None)
    if page_num > 1:
        params["page"] = [str(page_num)]
    new_query = urlencode({k: v[0] for k, v in params.items()})
    return urlunparse(parsed._replace(query=new_query))


# ─────────────────────────────────────────────
# Product parser  (Shopee internal API format)
# ─────────────────────────────────────────────
def _price(raw) -> float:
    if not raw:
        return 0.0
    return round(raw / 100_000, 0)   # Shopee encodes prices ×100 000


def _extract_items_from_recommend_v2(body: dict) -> list:
    """
    recommend_v2 shape:
      body.data.units[].data.item[]   (each unit is a widget/row)
    Items may be wrapped in item_basic or be flat dicts with itemid.
    """
    items = []
    data  = body.get("data") or {}
    units = data.get("units") or []

    for unit in units:
        unit_data  = unit.get("data") or {}
        candidates = unit_data.get("item") or unit_data.get("items") or []
        for entry in candidates:
            if "item_basic" not in entry and "itemid" in entry:
                items.append({"item_basic": entry})
            else:
                items.append(entry)
    return items


def parse_api_items(items: list, page_num: int) -> list:
    products = []
    for item in items:
        if not item:
            continue
        info = item.get("item_basic", item)   # v4 wraps in item_basic

        rating_info   = info.get("item_rating") or {}
        rating_counts = rating_info.get("rating_count") or []
        total_ratings = sum(rating_counts) if rating_counts else 0

        shopid = info.get("shopid", "")
        itemid = info.get("itemid", "")

        products.append({
            "name":             info.get("name", "").strip(),
            "price_min":        _price(info.get("price_min") or info.get("price")),
            "price_max":        _price(info.get("price_max") or info.get("price")),
            "currency":         "VND",
            "rating":           round(float(rating_info.get("rating_star") or 0), 1),
            "rating_count":     total_ratings,
            "sold":             info.get("historical_sold") or info.get("sold") or 0,
            "stock":            info.get("stock", 0),
            "shop_name":        info.get("shop_name", ""),
            "shopid":           shopid,
            "itemid":           itemid,
            "url":              f"https://shopee.vn/product/{shopid}/{itemid}" if shopid and itemid else "",
            "image":            f"https://cf.shopee.vn/file/{info['image']}" if info.get("image") else "",
            "liked_count":      info.get("liked_count", 0),
            "is_official_shop": bool(info.get("is_official_shop", False)),
            "brand":            info.get("brand") or "",
            "location":         info.get("shop_location") or "",
            "page":             page_num,
            "scraped_at":       datetime.now().isoformat(),
        })
    return products


# ─────────────────────────────────────────────
# DOM fallback  (uses page.evaluate for speed + reliability)
# ─────────────────────────────────────────────
_DOM_EXTRACT_JS = r"""
() => {
    const items = document.querySelectorAll('li[data-sqe="item"]');
    const results = [];
    items.forEach(li => {
        // Skip skeleton/loading placeholders (no role="group" aria-label)
        const card = li.querySelector('[role="group"][aria-label^="Product card:"]');
        if (!card) return;

        // Name from aria-label — most reliable, avoids flag-label images
        const aria  = card.getAttribute('aria-label') || '';
        const name  = aria.startsWith('Product card:')
                      ? aria.slice('Product card: '.length).trim()
                      : '';

        // URL + shopid/itemid
        const link  = li.querySelector('a.contents');
        const href  = link ? link.getAttribute('href') || '' : '';
        const idm   = href.match(/i\\.(\d+)\\.(\d+)/);
        const shopid  = idm ? idm[1] : '';
        const itemid  = idm ? idm[2] : '';
        const url     = href ? 'https://shopee.vn' + href.split('?')[0] : '';

        // Price number — inside the price primary container
        let price = 0;
        const pricePrimary = li.querySelector('.text-shopee-primary');
        if (pricePrimary) {
            const spans = pricePrimary.querySelectorAll('span');
            for (const s of spans) {
                const t = s.textContent.trim();
                // Match Vietnamese price format: digits + dots, e.g. "244.000"
                if (/^[\d.]+$/.test(t) && t.length >= 3) {
                    price = parseFloat(t.replace(/\\./g, ''));
                    break;
                }
            }
        }

        // Sold count text  e.g. "40k+ sold" or "118 sold"
        let sold = '';
        const soldCandidates = li.querySelectorAll(
            '.text-shopee-black87.text-xs'
        );
        soldCandidates.forEach(el => {
            const t = el.textContent.trim();
            if (t.includes('sold')) sold = t;
        });

        // Discount badge  e.g. aria-label="-26%"
        const discEl  = li.querySelector('span[aria-label^="-"]');
        const discount = discEl ? discEl.getAttribute('aria-label') : '';

        // Product thumbnail (width=320 img, NOT the overlay img)
        const imgEl = li.querySelector('img[width="320"]');
        const image = imgEl ? imgEl.getAttribute('src') || '' : '';

        // Promo label text  e.g. "Selling Fast", "Flash Sale …", "Cheap on Shopee"
        let promo = '';
        const flashEl = li.querySelector('.text-shopee-primary .truncate');
        if (flashEl) promo = flashEl.textContent.trim();
        if (!promo) {
            const sellingEl = li.querySelector('[style*="255, 255, 255"] span');
            if (sellingEl) promo = sellingEl.textContent.trim();
        }

        if (name) {
            results.push({ name, price, sold, discount, promo,
                           shopid, itemid, url, image });
        }
    });
    return results;
}
"""

async def scrape_dom_fallback(page, page_num: int) -> list:
    try:
        await page.wait_for_selector('li[data-sqe="item"]', timeout=12_000)
    except PlaywrightTimeout:
        return []

    raw = await page.evaluate(_DOM_EXTRACT_JS)
    now = datetime.now().isoformat()
    return [
        {**item, "currency": "VND", "page": page_num, "scraped_at": now}
        for item in (raw or [])
    ]


# ─────────────────────────────────────────────
# Main scrape loop
# ─────────────────────────────────────────────
async def scrape(playwright, url: str, max_pages: int, output: str):
    print(f"\nTarget : {url}")
    print(f"Pages  : up to {max_pages}\n")

    context     = await open_persistent_browser(playwright)
    page        = await context.new_page()
    all_products: list = []
    api_bucket:   list = []
    raw_responses: list = []   # stores raw recommend_v2 bodies

    async def on_response(response):
        url_r = response.url
        is_recommend  = "api/v4/recommend/recommend_v2" in url_r
        is_search     = not is_recommend and any(p in url_r for p in [
                            "api/v4/search/search_items",
                            "api/v2/search_items",
                        ])

        if not (is_recommend or is_search):
            return
        try:
            if response.status != 200:
                return
            body = await response.json()

            if is_recommend:
                raw_responses.append(body)
                print(f"  [API:recommend_v2] raw response saved ({len(raw_responses)} total)")
                # also parse items for CSV/JSON output
                items = _extract_items_from_recommend_v2(body)
            else:
                items = ((body.get("data") or {}).get("items")
                         or body.get("items") or [])

            if items:
                api_bucket.clear()
                api_bucket.extend(items)
        except Exception as exc:
            print(f"  [API] parse error: {exc}")

    page.on("response", on_response)

    for page_num in range(1, max_pages + 1):
        api_bucket.clear()
        nav_url = build_page_url(url, page_num)
        print(f"[Page {page_num}] {nav_url}")

        try:
            await page.goto(nav_url, wait_until="domcontentloaded", timeout=35_000)
        except PlaywrightTimeout:
            print(f"  Timeout on page {page_num}, stopping.")
            break

        await human_delay(1500, 3200)
        await scroll_naturally(page, rounds=7)
        await human_delay(800, 2000)

        if api_bucket:
            products = parse_api_items(api_bucket, page_num)
        else:
            print("  API interception yielded nothing — trying DOM fallback.")
            products = await scrape_dom_fallback(page, page_num)

        if not products:
            print(f"  No products on page {page_num}. Stopping pagination.")
            break

        all_products.extend(products)
        print(f"  {len(products)} products  (total so far: {len(all_products)})")

        if page_num < max_pages:
            await human_delay(1800, 4000)

    await context.close()

    # Save raw recommend_v2 responses
    if raw_responses:
        stem = re.sub(r"\.(csv|json)$", "", output, flags=re.IGNORECASE)
        raw_path = f"{stem}_raw.json"
        with open(raw_path, "w", encoding="utf-8") as f:
            json.dump(raw_responses, f, ensure_ascii=False, indent=2)
        print(f"\nRaw API responses saved → {raw_path} ({len(raw_responses)} pages)")

    if all_products:
        _save_results(all_products, output)
        print(f"Done — {len(all_products)} products saved.")
    else:
        print("\nNo structured products captured (raw responses saved above).")


# ─────────────────────────────────────────────
# Save results
# ─────────────────────────────────────────────
def _save_results(products: list, base_name: str):
    stem      = re.sub(r"\.(csv|json)$", "", base_name, flags=re.IGNORECASE)
    csv_path  = f"{stem}.csv"
    json_path = f"{stem}.json"

    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=products[0].keys())
        writer.writeheader()
        writer.writerows(products)

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(products, f, ensure_ascii=False, indent=2)

    print(f"  -> {csv_path}")
    print(f"  -> {json_path}")


# ─────────────────────────────────────────────
# Login mode
# ─────────────────────────────────────────────
async def do_login(playwright):
    print("\n=== LOGIN MODE ===")
    print("Browser opens at shopee.vn/buyer/login.")
    print("Log in however Shopee asks. Once on the homepage, press Enter here.\n")

    context = await open_persistent_browser(playwright)
    page    = await context.new_page()
    await page.goto("https://shopee.vn/buyer/login", wait_until="domcontentloaded")

    input(">>> Press Enter after you are fully logged in and on the homepage: ")

    # Warm up the session cookies
    try:
        await page.goto("https://shopee.vn/", wait_until="domcontentloaded", timeout=20_000)
        await human_delay(2000, 3500)
    except Exception:
        pass

    await context.close()
    print(f"\nSession persisted in: {PROFILE_DIR}")
    print("Run scraper.py --url ... to start scraping.")


# ─────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────
def parse_args():
    p = argparse.ArgumentParser(
        description="Shopee.vn scraper — login once, scrape anytime.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python scraper.py --login
  python scraper.py --url "https://shopee.vn/search?keyword=laptop" --pages 10
  python scraper.py --url "https://shopee.vn/Thoi-Trang-Nam-cat.11035567" --pages 5 --output fashion
""",
    )
    p.add_argument("--login",  action="store_true", help="Open browser to log in")
    p.add_argument("--url",    type=str, nargs="+", help="One or more Shopee URLs to scrape")
    p.add_argument("--pages",  type=int, default=7, help="Max pages per URL (default 7)")
    p.add_argument(
        "--output",
        type=str,
        default=None,
        help="Output filename stem. With multiple URLs, each gets its own file (url_1, url_2, …). "
             "Defaults to products_TIMESTAMP[_N].",
    )
    return p.parse_args()


def _output_stem(base: str | None, index: int, total: int) -> str:
    """Return output stem for URL at position `index` (0-based)."""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    if base:
        return base if total == 1 else f"{base}_{index + 1}"
    return f"products_{ts}" if total == 1 else f"products_{ts}_{index + 1}"


async def main():
    args = parse_args()
    async with async_playwright() as pw:
        if args.login:
            await do_login(pw)
        elif args.url:
            urls  = args.url
            total = len(urls)
            for i, url in enumerate(urls):
                if total > 1:
                    print(f"\n{'='*60}")
                    print(f"URL {i+1}/{total}: {url}")
                    print('='*60)
                stem = _output_stem(args.output, i, total)
                await scrape(pw, url, args.pages, stem)
        else:
            print("Provide --login or --url. Use --help for examples.")


if __name__ == "__main__":
    asyncio.run(main())
