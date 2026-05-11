"""Tar fullside-screenshots av berlingske.dk og aftenposten.no.
Lagres i en lokal mappe `out/`. GitHub Actions tar seg av opplastingen."""
import asyncio
import os
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path
from playwright.async_api import async_playwright

OUTPUT_DIR = Path('out')
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

TZ = ZoneInfo('Europe/Oslo')

USER_AGENT = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
)

SITES = [
    {
        'name': 'berlingske',
        'url':  'https://www.berlingske.dk/',
        'consent_texts': [
            'Acceptér alle', 'Accepter alle', 'Accepter Alle',
            'Acceptér alle og luk', 'Accepter alle og luk',
            'Godkend alle', 'Tillad alle', 'OK for mig', 'Jeg accepterer',
        ],
        'consent_css': [
            'button.message-button',
            'button[title*="ccept" i]',
            'button[aria-label*="ccept" i]',
        ],
        'iframe_url_hint': ['sourcepoint', 'consensu', 'cmp'],
    },
    {
        'name': 'aftenposten',
        'url':  'https://www.aftenposten.no/',
        'consent_texts': [
            'Godta alle', 'Aksepter alle', 'Godta', 'Godkjenn alle', 'Godkjenn',
            'Tillat alle',
        ],
        'consent_css': [
            'button.message-button',
            'button[title*="odta" i]',
            'button[aria-label*="odta" i]',
            'button[data-testid*="accept" i]',
        ],
        'iframe_url_hint': ['sourcepoint', 'consensu', 'cmp', 'schibsted'],
    },
]


async def try_click(scope, by_text, by_css):
    for txt in by_text:
        try:
            btn = scope.get_by_role('button', name=txt)
            if await btn.count() > 0 and await btn.first.is_visible(timeout=400):
                await btn.first.click(timeout=2000)
                return f'role-button:{txt}'
        except Exception:
            pass
    for css in by_css:
        try:
            loc = scope.locator(css)
            if await loc.count() > 0 and await loc.first.is_visible(timeout=400):
                txt = (await loc.first.inner_text(timeout=500)).strip().lower()
                if any(w in txt for w in ['accept', 'godta', 'godkjen',
                                          'tillat', 'godkend', 'ok ', 'alle']):
                    await loc.first.click(timeout=2000)
                    return f'css:{css}={txt[:30]}'
        except Exception:
            pass
    return None


async def dismiss_consent(page, site):
    log = []
    await page.wait_for_timeout(3500)
    res = await try_click(page, site['consent_texts'], site['consent_css'])
    if res:
        log.append(f'main:{res}')
        await page.wait_for_timeout(1500)
        return ' + '.join(log)

    cmp_frames = [
        f for f in page.frames
        if f != page.main_frame and any(h in (f.url or '').lower() for h in site['iframe_url_hint'])
    ]
    if not cmp_frames:
        cmp_frames = [f for f in page.frames if f != page.main_frame]
    log.append(f'iframes-funnet:{len(cmp_frames)}')
    for frame in cmp_frames:
        res = await try_click(frame, site['consent_texts'], site['consent_css'])
        if res:
            log.append(f'iframe:{res}')
            await page.wait_for_timeout(1500)
            return ' + '.join(log)

    try:
        clicked = await page.evaluate("""
            (texts) => {
                const allButtons = [
                    ...document.querySelectorAll('button'),
                    ...document.querySelectorAll('[role="button"]'),
                    ...document.querySelectorAll('a.btn'),
                ];
                for (const btn of allButtons) {
                    const t = (btn.innerText || btn.textContent || '').trim().toLowerCase();
                    for (const txt of texts) {
                        if (t.includes(txt.toLowerCase())) {
                            btn.click();
                            return t.slice(0, 40);
                        }
                    }
                }
                return null;
            }
        """, site['consent_texts'])
        if clicked:
            log.append(f'js:{clicked}')
            await page.wait_for_timeout(1500)
            return ' + '.join(log)
    except Exception as e:
        log.append(f'js-error:{type(e).__name__}')

    return ' + '.join(log) + ' [INGEN KLIKK]'


async def trigger_lazy_loading(page):
    await page.evaluate("""
        async () => {
            const totalHeight = document.body.scrollHeight;
            for (let y = 0; y < totalHeight; y += 600) {
                window.scrollTo(0, y);
                await new Promise(r => setTimeout(r, 80));
            }
            window.scrollTo(0, 0);
        }
    """)
    await page.wait_for_timeout(2000)


async def capture_site(playwright, site, timestamp):
    out_path = OUTPUT_DIR / f"{site['name']}_{timestamp}.png"
    print(f"\n  === {site['name']} ===")
    browser = await playwright.chromium.launch(
        headless=True, args=['--no-sandbox', '--disable-dev-shm-usage'],
    )
    context = await browser.new_context(
        viewport={'width': 1440, 'height': 900},
        device_scale_factor=1,
        user_agent=USER_AGENT,
        locale='nb-NO',
        timezone_id='Europe/Oslo',
    )
    page = await context.new_page()
    try:
        print(f"  -> goto {site['url']}")
        await page.goto(site['url'], wait_until='domcontentloaded', timeout=45000)
        title = await page.title()
        print(f"  -> tittel: {title[:80]}")
        await page.wait_for_timeout(2000)
        consent = await dismiss_consent(page, site)
        print(f"  -> consent: {consent}")
        await page.wait_for_timeout(2500)
        await trigger_lazy_loading(page)
        await page.screenshot(path=str(out_path), full_page=True, timeout=120000)
        size_mb = out_path.stat().st_size / 1024 / 1024
        print(f"  -> OK   {out_path.name}  ({size_mb:.1f} MB)")
        return True
    except Exception as e:
        print(f"  -> FEIL  {type(e).__name__}: {e}")
        return False
    finally:
        await context.close()
        await browser.close()


async def main():
    timestamp = datetime.now(TZ).strftime('%Y-%m-%d_%H%M')
    print(f'[{timestamp}] Starter')
    success = 0
    async with async_playwright() as p:
        for site in SITES:
            if await capture_site(p, site, timestamp):
                success += 1
    print(f'\n[{timestamp}] Ferdig. {success}/{len(SITES)} vellykket.')
    if success == 0:
        raise SystemExit(1)


if __name__ == '__main__':
    asyncio.run(main())
