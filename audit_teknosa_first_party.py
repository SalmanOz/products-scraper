"""Runner proof of one selected, first-party Teknosa price; no writes."""

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from tempfile import gettempdir

from teknosa_reviewed_source import NAME, PRODUCT_ID, SPECS, URL, TeknosaReviewedSource


OUT = Path(os.environ.get('TEKNOSA_AUDIT_OUTPUT_DIR', gettempdir()))


def run_audit(source=None):
    offer, reason, status = (source or TeknosaReviewedSource()).read_offer(URL)
    return {
        'purpose': 'read_only_exact_first_party_teknosa_offer',
        'checked_at': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
        'catalog_id': 248, 'slug': 'apple-iphone-17e', 'name': NAME,
        'specs': SPECS, 'retailer_product_id': PRODUCT_ID, 'url': URL,
        'reason': reason, 'http_status': status, 'verified': offer is not None,
        'offer': {key: offer.get(key) for key in
                  ('merchant', 'price', 'currency', 'availability', 'source_title',
                   'url', 'observed_at')}
                 if offer else None,
        'writes': False,
    }


def main():
    report = run_audit()
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / 'teknosa-first-party-audit.json').write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + '\n')
    summary = ('## Teknosa selected first-party Apple iPhone 17e 8/256 (read only)\n'
               f"Verified: {report['verified']}; reason: {report['reason']}; "
               f"HTTP: {report['http_status']}; cash TRY: "
               f"{(report['offer'] or {}).get('price')}\n"
               'No catalog, price, or image writes.\n')
    (OUT / 'teknosa-first-party-audit.md').write_text(summary)
    print(summary, end='')
    return 0 if report['verified'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
