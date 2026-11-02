"""Read-only GitHub runner proof for one lower-priced exact Hepsiburada PDP."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from tempfile import gettempdir

from hepsiburada_s25_fe_source import HepsiburadaS25FeSource, URL


OUT = Path(os.environ.get('S25_FE_HEPSIBURADA_AUDIT_OUTPUT_DIR', gettempdir()))


def run_audit(source=None):
    offer, reason, http_status = (source or HepsiburadaS25FeSource()).read_offer()
    return {'purpose': 'read_only_exact_first_party_s25_fe_8_256_offer',
            'checked_at': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
            'url': URL, 'reason': reason, 'http_status': http_status,
            'verified': offer is not None,
            'offer': {key: offer.get(key) for key in
                      ('merchant', 'price', 'currency', 'availability', 'source_title', 'url')}
                     if offer else None,
            'writes': False}


def main():
    report = run_audit()
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / 's25-fe-hepsiburada-audit.json').write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + '\n')
    summary = ('## S25 FE 8/256 Hepsiburada exact PDP (read only)\n'
               f"Verified: {report['verified']}; {report['reason']} "
               f"HTTP {report['http_status']}; exact cash TRY "
               f"{(report['offer'] or {}).get('price')}.\n"
               'No catalog, price, or image writes.\n')
    (OUT / 's25-fe-hepsiburada-audit.md').write_text(summary)
    print(summary, end='')
    return 0 if report['verified'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
