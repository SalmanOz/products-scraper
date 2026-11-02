"""Runner access proof for one advertised Teknosa product sitemap shard."""

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from tempfile import gettempdir

from teknosa_sitemap_audit import TeknosaSitemapAudit


OUT = Path(os.environ.get('TEKNOSA_SITEMAP_AUDIT_OUTPUT_DIR', gettempdir()))


def main():
    report = TeknosaSitemapAudit().run()
    report['checked_at'] = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / 'teknosa-sitemap-audit.json').write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + '\n')
    lines = ['## Teknosa advertised product sitemap shard (read only)',
             f"Status: {report['status']}; stage: {report['stage']}; "
             f"error: {report.get('error')}; product URLs: {report['product_url_count']}; "
             f"browser requests: {report['browser_requests']}."]
    for url, detail in report['solver_diagnostics'].items():
        xml = report['xml_diagnostics'].get(url, {})
        lines.append(f"- {url.rsplit('/', 1)[-1]}: solver bytes "
                     f"{detail.get('received_bytes')}, response kind "
                     f"{detail.get('response_kind')}, response UTF-8 bytes "
                     f"{detail.get('response_utf8_bytes')}, extracted XML bytes "
                     f"{xml.get('xml_bytes')}.")
    for key, urls in report['candidate_urls'].items():
        lines.append(f'- {key}: {len(urls)} exact-pattern URL hints (up to 4 shown).')
    lines.append('URL hints only. No selected seller, stock, cash-price or catalog writes.')
    summary = '\n'.join(lines) + '\n'
    (OUT / 'teknosa-sitemap-audit.md').write_text(summary)
    print(summary, end='')
    return 0 if report['status'] == 'completed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
