"""IndexNow submission for teknoskor.com.

Pushes changed URLs to Bing/Yandex/Naver (any IndexNow-participating engine)
instead of waiting on their crawl schedules — high-value for a price-comparison
site whose product pages change daily. The key is self-issued: it just has to
match the contents of https://teknoskor.com/{KEY}.txt, which is committed at
frontend/public/{KEY}.txt in the products repo.
"""
import logging
import requests

INDEXNOW_KEY = "c0a4ec31b4c904b6e876ca6fc548f6c0"
BASE_URL = "https://teknoskor.com"
ENDPOINT = "https://api.indexnow.org/indexnow"


def submit_urls(paths):
    """Submit a list of site-relative paths (e.g. ['/product/foo']) to IndexNow.

    Never raises: indexing pings are best-effort and must not fail the
    price-update run that calls this.
    """
    urls = [f"{BASE_URL}{p}" for p in dict.fromkeys(paths) if p]
    if not urls:
        return
    # IndexNow accepts up to 10,000 URLs per POST; our catalog is far smaller,
    # but chunk anyway so a future catalog can't silently exceed the limit.
    for i in range(0, len(urls), 10000):
        chunk = urls[i:i + 10000]
        try:
            resp = requests.post(
                ENDPOINT,
                json={
                    "host": "teknoskor.com",
                    "key": INDEXNOW_KEY,
                    "keyLocation": f"{BASE_URL}/{INDEXNOW_KEY}.txt",
                    "urlList": chunk,
                },
                timeout=15,
            )
            # 200 = submitted, 202 = accepted (key not yet verified) — both fine
            if resp.status_code in (200, 202):
                logging.info(f"  📡 IndexNow: submitted {len(chunk)} URL(s) (HTTP {resp.status_code})")
            else:
                logging.warning(f"  ⚠️ IndexNow returned HTTP {resp.status_code}: {resp.text[:200]}")
        except Exception as e:
            logging.warning(f"  ⚠️ IndexNow submission failed (non-fatal): {e}")
