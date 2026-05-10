#!/usr/bin/env python3
"""
API Discovery Script — intercepts ALL network requests from 4 car sites.
Navigates to each site, performs a car search, captures every XHR/fetch/API call.
Saves results to JSON for analysis.

Usage:
    python discover_apis.py

Output:
    discovered_apis.json — all intercepted API requests grouped by site
"""

import asyncio
import json
import re
import time
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import async_playwright, Route, Request, Response


# Sites to probe
TARGETS = [
    {
        "name": "autoscout24",
        "search_url": "https://www.autoscout24.com/lst/bmw/3er?sort=standard&desc=0&ustate=N%2CU&fregfrom=2020&priceto=40000",
        "wait_selector": "article",
        "wait_timeout": 15000,
    },
    {
        "name": "mobile.de",
        "search_url": "https://suchen.mobile.de/fahrzeuge/search.html?dam=0&isSearchRequest=true&ms=3500;;;&minFirstRegistrationDate=2020-01-01&maxPrice=40000&vc=Car&sb=doc&od=down",
        "wait_selector": "a.result-item, .cBox-body--resultitem, [class*='result']",
        "wait_timeout": 15000,
    },
    {
        "name": "blocket",
        "search_url": "https://www.blocket.se/annonser/hela_sverige/fordon/bilar?cg=1020&q=bmw+3&sort=date",
        "wait_selector": "article, [data-testid*='ListItem'], [class*='AdCard']",
        "wait_timeout": 15000,
    },
    {
        "name": "bytbil",
        "search_url": "https://www.bytbil.com/bil?VehicleType=bil&Makes=BMW&FreeText=3+Series&SortParams.SortField=publishedDate&SortParams.IsAscending=False",
        "wait_selector": "article, .result-list, .vehicle-card, [class*='listing']",
        "wait_timeout": 15000,
    },
]

# Filter: only keep interesting requests (API calls, JSON, GraphQL)
INTERESTING_CONTENT_TYPES = {"application/json", "application/graphql", "text/json"}
INTERESTING_URL_PATTERNS = [
    r"/api/", r"/graphql", r"/_next/data/", r"/search", r"/listing",
    r"/vehicle", r"/car", r"/ad", r"/result", r"/mobility",
    r"/refdata/", r"/query", r"\.json", r"/v1/", r"/v2/",
]
SKIP_PATTERNS = [
    r"google", r"facebook", r"analytics", r"tracking", r"consent",
    r"gtm\.js", r"pixel", r"beacon", r"sentry", r"datadog",
    r"newrelic", r"hotjar", r"clarity", r"segment", r"amplitude",
    r"fonts\.", r"\.css", r"\.woff", r"\.png", r"\.jpg", r"\.svg",
    r"\.gif", r"\.ico", r"adtech", r"prebid", r"doubleclick",
]


def is_interesting(url: str, content_type: str = "") -> bool:
    """Check if a request is likely an API call (not tracking/analytics/static)."""
    url_lower = url.lower()

    # Skip obvious non-API
    for skip in SKIP_PATTERNS:
        if re.search(skip, url_lower):
            return False

    # Check content type
    if content_type:
        for ct in INTERESTING_CONTENT_TYPES:
            if ct in content_type.lower():
                return True

    # Check URL patterns
    for pattern in INTERESTING_URL_PATTERNS:
        if re.search(pattern, url_lower):
            return True

    return False


async def discover_site(playwright, target: dict) -> dict:
    """Navigate to a car site, perform search, capture all API requests."""
    name = target["name"]
    print(f"\n{'='*60}")
    print(f"  Discovering APIs: {name}")
    print(f"{'='*60}")

    captured_requests = []
    captured_responses = []

    browser = await playwright.chromium.launch(
        headless=True,
        args=[
            "--no-sandbox",
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
        ],
    )

    context = await browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.0.0 Safari/537.36"
        ),
        viewport={"width": 1920, "height": 1080},
        locale="de-DE",
        # Record HAR for full analysis
        record_har_path=f"har_{name}.har",
        record_har_url_filter=re.compile(r".*"),
    )

    page = await context.new_page()

    # Intercept ALL requests
    async def on_request(request: Request):
        url = request.url
        if is_interesting(url):
            entry = {
                "url": url,
                "method": request.method,
                "headers": dict(request.headers),
                "post_data": None,
                "resource_type": request.resource_type,
            }
            if request.method == "POST":
                try:
                    entry["post_data"] = request.post_data
                except:
                    pass
            captured_requests.append(entry)
            print(f"  [{name}] → {request.method} {url[:120]}")

    # Intercept ALL responses
    async def on_response(response: Response):
        url = response.url
        content_type = response.headers.get("content-type", "")
        if is_interesting(url, content_type):
            entry = {
                "url": url,
                "status": response.status,
                "content_type": content_type,
                "headers": dict(response.headers),
                "body_preview": None,
            }
            # Try to capture JSON response body
            if "json" in content_type.lower() or "graphql" in content_type.lower():
                try:
                    body = await response.text()
                    # Truncate large responses
                    if len(body) > 5000:
                        entry["body_preview"] = body[:5000] + f"... [truncated, total {len(body)} chars]"
                        entry["body_size"] = len(body)
                    else:
                        entry["body_preview"] = body
                except:
                    entry["body_preview"] = "[failed to read body]"
            captured_responses.append(entry)

    page.on("request", on_request)
    page.on("response", on_response)

    # Navigate to search page
    print(f"  [{name}] Navigating to: {target['search_url'][:80]}...")
    try:
        await page.goto(target["search_url"], wait_until="domcontentloaded", timeout=30000)

        # Wait for results to load
        try:
            await page.wait_for_selector(target["wait_selector"], timeout=target["wait_timeout"])
            print(f"  [{name}] Results loaded!")
        except:
            print(f"  [{name}] Wait for selector timed out (page may still have loaded)")

        # Wait extra for lazy-loaded API calls
        await page.wait_for_timeout(5000)

        # Try scrolling to trigger more API calls
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight / 2)")
        await page.wait_for_timeout(2000)
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await page.wait_for_timeout(2000)

        # Extract __NEXT_DATA__ if present
        next_data = await page.evaluate("""
            () => {
                const el = document.querySelector('script#__NEXT_DATA__');
                if (el) {
                    try {
                        const data = JSON.parse(el.textContent);
                        return {
                            buildId: data.buildId || null,
                            topKeys: Object.keys(data),
                            propsKeys: data.props ? Object.keys(data.props) : [],
                            pagePropsKeys: data.props?.pageProps ? Object.keys(data.props.pageProps) : [],
                            // Check for listing data
                            hasListings: !!(data.props?.pageProps?.listings || data.props?.pageProps?.searchResult),
                            listingsCount: (data.props?.pageProps?.listings || []).length,
                            // Sample first listing keys if available
                            firstListingKeys: data.props?.pageProps?.listings?.[0]
                                ? Object.keys(data.props.pageProps.listings[0])
                                : [],
                        };
                    } catch (e) { return { error: e.message }; }
                }
                return null;
            }
        """)

        # Extract any window.__INITIAL_STATE__ or similar
        embedded_data = await page.evaluate("""
            () => {
                const found = {};
                // Common embedded data patterns
                const patterns = [
                    '__NEXT_DATA__', '__INITIAL_STATE__', '__APOLLO_STATE__',
                    '__RELAY_STORE__', '__PRELOADED_STATE__', '__DATA__',
                    'window.__data', 'window.__config', 'window._sharedData',
                ];
                for (const key of Object.keys(window)) {
                    if (key.startsWith('__') && key.endsWith('__') && key !== '__proto__') {
                        try {
                            const val = window[key];
                            if (val && typeof val === 'object') {
                                found[key] = Object.keys(val).slice(0, 20);
                            }
                        } catch (e) {}
                    }
                }
                return found;
            }
        """)

        # Count visible listings
        listing_count = await page.evaluate("""
            () => {
                const selectors = ['article', '[class*="result"]', '[class*="listing"]', '[class*="AdCard"]', '[class*="vehicle"]'];
                for (const sel of selectors) {
                    const els = document.querySelectorAll(sel);
                    if (els.length > 2) return { selector: sel, count: els.length };
                }
                return { selector: null, count: 0 };
            }
        """)

        # Get all cookies (including Akamai _abck)
        cookies = await context.cookies()
        interesting_cookies = [
            {"name": c["name"], "domain": c["domain"], "value": c["value"][:50] + "..." if len(c["value"]) > 50 else c["value"]}
            for c in cookies
            if any(k in c["name"].lower() for k in ["abck", "bm_", "session", "token", "auth", "csrf", "ak_", "datadome"])
        ]

    except Exception as e:
        print(f"  [{name}] ERROR: {e}")
        next_data = None
        embedded_data = {}
        listing_count = {"selector": None, "count": 0}
        interesting_cookies = []

    await context.close()
    await browser.close()

    result = {
        "site": name,
        "search_url": target["search_url"],
        "requests_captured": len(captured_requests),
        "responses_captured": len(captured_responses),
        "api_requests": captured_requests,
        "api_responses": captured_responses,
        "next_data": next_data,
        "embedded_data": embedded_data,
        "listing_count": listing_count,
        "interesting_cookies": interesting_cookies,
    }

    print(f"\n  [{name}] Summary:")
    print(f"    API requests captured: {len(captured_requests)}")
    print(f"    API responses captured: {len(captured_responses)}")
    print(f"    __NEXT_DATA__: {'YES' if next_data else 'NO'}")
    print(f"    Embedded data keys: {list(embedded_data.keys()) if embedded_data else 'none'}")
    print(f"    Listings found: {listing_count}")
    print(f"    Interesting cookies: {[c['name'] for c in interesting_cookies]}")

    return result


async def main():
    print("=" * 60)
    print("  API DISCOVERY TOOL — 4 Car Marketplaces")
    print("  Intercepts all network requests during car search")
    print("=" * 60)

    async with async_playwright() as pw:
        all_results = {}
        for target in TARGETS:
            try:
                result = await discover_site(pw, target)
                all_results[target["name"]] = result
            except Exception as e:
                print(f"\n  ERROR on {target['name']}: {e}")
                all_results[target["name"]] = {"error": str(e)}

            # Brief pause between sites
            await asyncio.sleep(2)

    # Save results
    output_path = Path(__file__).parent / "discovered_apis.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(all_results, f, indent=2, ensure_ascii=False, default=str)

    print(f"\n{'='*60}")
    print(f"  Results saved to: {output_path}")
    print(f"{'='*60}")

    # Print summary
    print(f"\n{'='*60}")
    print("  SUMMARY")
    print(f"{'='*60}")
    for site_name, data in all_results.items():
        if "error" in data:
            print(f"\n  {site_name}: ERROR — {data['error']}")
            continue
        print(f"\n  {site_name}:")
        print(f"    API requests: {data['requests_captured']}")
        print(f"    __NEXT_DATA__: {'YES' if data.get('next_data') else 'NO'}")
        if data.get("next_data"):
            nd = data["next_data"]
            print(f"    BuildId: {nd.get('buildId')}")
            print(f"    Has listings: {nd.get('hasListings')} ({nd.get('listingsCount', 0)} items)")
            print(f"    First listing keys: {nd.get('firstListingKeys', [])[:10]}")
        print(f"    Cookies: {[c['name'] for c in data.get('interesting_cookies', [])]}")

        # Show top API endpoints
        api_urls = set()
        for req in data.get("api_requests", []):
            parsed = urlparse(req["url"])
            api_urls.add(f"{req['method']} {parsed.scheme}://{parsed.netloc}{parsed.path}")
        if api_urls:
            print(f"    Key API endpoints:")
            for url in sorted(api_urls)[:15]:
                print(f"      {url}")

    # Also save HAR files list
    print(f"\n  HAR files saved: har_autoscout24.har, har_mobile.de.har, har_blocket.har, har_bytbil.har")
    print(f"  Analyze with: python -c \"import json; data=json.load(open('har_autoscout24.har')); print([e['request']['url'] for e in data['log']['entries'] if '/api/' in e['request']['url'] or 'graphql' in e['request']['url']])\"")


if __name__ == "__main__":
    asyncio.run(main())
