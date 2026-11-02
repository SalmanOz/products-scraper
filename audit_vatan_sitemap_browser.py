"""Read-only runner pilot of the production Vatan sitemap discovery path."""

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from tempfile import gettempdir

from audit_vatan_source import PROFILES
from vatan_sitemap_source import VatanSitemapSource


OUT = Path(os.environ.get('VATAN_SITEMAP_AUDIT_OUTPUT_DIR', gettempdir()))


def run_audit(source=None):
    source = source or VatanSitemapSource()
    products = []
    for profile in PROFILES:
        specs = {key: profile[key] for key in ('ram_gb', 'storage_gb', 'network_generation')}
        products.append({'slug': profile['slug'],
                         'candidate_urls': [row['source_url'] for row in
                                            source.discover(profile['name'], specs)]})
    status = source.report()
    return {'purpose': 'read_only_vatan_sitemap_discovery_pilot',
            'checked_at': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
            'sitemap': status, 'products': products,
            'verified_discovery': status['status'] == 'ready'
                                  and any(row['candidate_urls'] for row in products),
            'writes': False}


def summary(report):
    status = report['sitemap']
    lines = ['## Vatan sitemap discovery pilot (read only)',
             f"Status: {status['status']}; direct HTTP {status['sitemap_direct_http_status']}; "
             f"transport {status['sitemap_transport']}; browser GET {status['browser_requests']}; "
             f"eligible phone URLs {status['eligible_urls']}."]
    lines += [f"- {row['slug']}: {len(row['candidate_urls'])} exact URL hints."
              for row in report['products']]
    lines.append('No PDP price, catalog, ingestion, schema, R2, or IndexNow writes.')
    return '\n'.join(lines) + '\n'


def main():
    report = run_audit()
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / 'vatan-sitemap-browser.json').write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    text = summary(report)
    (OUT / 'vatan-sitemap-browser.md').write_text(text)
    print(text, end='')
    return 0 if report['verified_discovery'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
