"""Read-only runner proof for three exact first-party Apple PDPs."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from tempfile import gettempdir

from hepsiburada_apple_source import HepsiburadaAppleSource, REVIEWED_PDPS


OUT = Path(os.environ.get('HEPSIBURADA_APPLE_AUDIT_OUTPUT_DIR', gettempdir()))


def run_audit(source=None):
    reader = source or HepsiburadaAppleSource()
    rows = []
    for target in REVIEWED_PDPS:
        offer, reason, status = reader.read_offer(target.url)
        rows.append({
            'id': target.catalog_id, 'slug': target.slug, 'name': target.name,
            'ram_gb': target.ram_gb, 'storage_gb': 256, 'url': target.url,
            'reason': reason, 'http_status': status, 'verified': offer is not None,
            'offer': {key: offer.get(key) for key in
                      ('merchant', 'price', 'currency', 'availability', 'source_title', 'url')}
                     if offer else None,
        })
    return {
        'purpose': 'read_only_exact_first_party_hepsiburada_apple_offers',
        'checked_at': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
        'verified': all(row['verified'] for row in rows), 'products': rows,
        'writes': False,
    }


def main():
    report = run_audit()
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / 'hepsiburada-apple-audit.json').write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + '\n')
    lines = ['## Hepsiburada Apple exact first-party PDPs (read only)',
             f"All verified: {report['verified']}"]
    for row in report['products']:
        lines.append(f"- {row['slug']} {row['ram_gb']}/256: {row['reason']}; "
                     f"HTTP {row['http_status']}; cash TRY "
                     f"{(row['offer'] or {}).get('price')}")
    lines.append('No catalog, price, or image writes.')
    summary = '\n'.join(lines) + '\n'
    (OUT / 'hepsiburada-apple-audit.md').write_text(summary)
    print(summary, end='')
    return 0 if report['verified'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
