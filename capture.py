"""Tar fullside-screenshots, HTML og lenker fra Amedia-fronter.
Lagres i en lokal mappe `out/`. GitHub Actions tar seg av opplastingen."""
import asyncio
import csv
import json
import os
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path
from playwright.async_api import async_playwright

OUTPUT_DIR = Path('out')
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
RUNLOG = OUTPUT_DIR / 'runlog.csv'

TZ = ZoneInfo('Europe/Oslo')
CONCURRENCY = int(os.environ.get('CONCURRENCY', '3'))

USER_AGENT = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
)

# Annonse- og trackingdomener som blokkeres for å redusere lastetid
BLOCK_PATTERNS = [
    'doubleclick.net', 'googletagmanager', 'google-analytics',
    'facebook.com/tr', 'facebook.net', 'adservice', 'adnxs.com',
    'scorecardresearch', 'chartbeat', 'hotjar', 'segment.io',
    'amplitude', 'mixpanel', 'taboola', 'outbrain',
]

# Amedia bruker samme consent på alle sider - én liste er nok
CONSENT_SELECTORS = [
    'button:has-text("Godta alle")',
    'button:has-text("Godta")',
    'button:has-text("Aksepter alle")',
    'button:has-text("Aksepter")',
    'button:has-text("Tillat alle")',
    '[aria-label*="onsent"] button',
    '[title*="onsent"] button',
]

# site_key matcher konvensjonen i BigQuery
SITES = {
    'avnord': 'https://www.an.no',
    'bergen': 'https://www.ba.no',
    'budsti': 'https://www.budstikka.no',
    'dramti': 'https://www.dt.no',
    'frblad': 'https://www.f-b.no',
    'hamarb': 'https://www.h-a.no',
    'h_avis': 'https://www.h-avis.no',
    'nrdlys': 'https://www.nordlys.no',
    'opplan': 'https://www.oa.no',
    'rombla': 'https://www.rb.no',
    'telema': 'https://www.ta.no',
    'tonsbb': 'https://www.tb.no',
}


async def block_heavy(route):
    if any(p in route.request.url for p in BLOCK_PATTERNS):
        await route.abort()
    else:
        await route.continue_()


async def dismiss_consent(page):
    for sel in CONSENT_SELECTORS:
        try:
            await page.locator(sel).first.click(timeout=1500)
            await page.wait_for_timeout(500)
            return sel
        except Exception:
            continue
    return None


async def trigger_lazy_loading(page):
    await page.evaluate("""
        async () => {
            const step = 1500;
            const delay = 250;
            for (let y = 0; y < 40000; y += step) {
                window.scrollTo(0, y);
                await new Promise(r => setTimeout(r, delay));
                if (y > document.documentElement.scrollHeight) break;
            }
            window.scrollTo(0, 0);
            await new Promise(r => setTimeout(r, 1000));
        }
    """)


async def extract_links(page):
    return await page.eval_on_selector_all(
        "a[href]",
        """els => els.map(e => {
            const rect = e.getBoundingClientRect();
            return {
                href: e.href,
                text: (e.innerText || '').trim().slice(0, 200),
                y: Math.round(rect.top + window.scrollY),
                x: Math.round(rect.left)
            };
        })"""
    )


async def capture_site(browser, site_key, url, timestamp):
    stem = OUTPUT_DIR / f"{site_key}_{timestamp}"
    print(f"\n  === {site_key} ===")
    result = {
        'timestamp': datetime.now(TZ).isoformat(timespec='seconds'),
        'site_key': site_key,
        'url': url,
        'status': 'ok',
        'n_links': 0,
        'consent_selector': '',
        'error': '',
    }
    context = None
    try:
        context = await browser.new_context(
            viewport={'width': 1440, 'height': 900},
            device_scale_factor=1,
            user_agent=USER_AGENT,
            locale='nb-NO',
            timezone_id='Europe/Oslo',
        )
        await context.route('**/*', block_heavy)
        page = await context.new_page()

        print(f"  -> goto {url}")
        await page.goto(url, wait_until='load', timeout=45000)
        await page.wait_for_timeout(2000)

        matched = await dismiss_consent(page)
        result['consent_selector'] = matched or ''
        print(f"  -> consent: {matched or 'ingen match'}")

        await page.wait_for_timeout(2500)
        await trigger_lazy_loading(page)

        await page.screenshot(path=str(stem) + '.png', full_page=True, timeout=120000)
        Path(str(stem) + '.html').write_text(await page.content(), encoding='utf-8')

        links = await extract_links(page)
        Path(str(stem) + '_links.json').write_text(
            json.dumps(links, ensure_ascii=False, indent=2),
            encoding='utf-8'
        )
        result['n_links'] = len(links)

        png_size_mb = Path(str(stem) + '.png').stat().st_size / 1024 / 1024
        print(f"  -> OK   {site_key}  ({png_size_mb:.1f} MB, {len(links)} lenker)")
    except Exception as e:
        result['status'] = 'error'
        result['error'] = f"{type(e).__name__}: {str(e)[:200]}"
        print(f"  -> FEIL  {result['error']}")
    finally:
        if context is not None:
            try:
                await context.close()
            except Exception:
                pass
    return result


def append_log(row, logfile):
    new_file = not logfile.exists()
    with logfile.open('a', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=list(row.keys()))
        if new_file:
            writer.writeheader()
        writer.writerow(row)


async def main():
    timestamp = datetime.now(TZ).strftime('%Y-%m-%d_%H%M')
    print(f'[{timestamp}] Starter - {len(SITES)} aviser, {CONCURRENCY} parallelt')

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=['--no-sandbox', '--disable-dev-shm-usage'],
        )
        sem = asyncio.Semaphore(CONCURRENCY)

        async def bounded(site_key, url):
            async with sem:
                return await capture_site(browser, site_key, url, timestamp)

        tasks = [bounded(k, u) for k, u in SITES.items()]
        results = await asyncio.gather(*tasks)
        await browser.close()

    for r in results:
        append_log(r, RUNLOG)

    success = sum(1 for r in results if r['status'] == 'ok')
    print(f'\n[{timestamp}] Ferdig. {success}/{len(SITES)} vellykket.')
    if success == 0:
        raise SystemExit(1)


if __name__ == '__main__':
    asyncio.run(main())
