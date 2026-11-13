"""Check that a verified earbud snapshot preserves the published catalog identities."""

from datetime import datetime
import json
from pathlib import Path
import sys


original, refreshed, report = (json.loads(Path(path).read_bytes()) for path in sys.argv[1:])
assert {key: value for key, value in refreshed.items() if key not in {'models', 'checked_at', 'sourceTargets'}} == {
    key: value for key, value in original.items() if key not in {'models', 'checked_at', 'sourceTargets'}
}
before = datetime.fromisoformat(original['checked_at'])
assert datetime.fromisoformat(refreshed['checked_at']) >= before
mode = report['publicationMode']
assert mode in {'full', 'verified_variants_only'}
changed = []
for old, new in zip(original['models'], refreshed['models'], strict=True):
    assert {key: value for key, value in old.items() if key != 'offers'} == {
        key: value for key, value in new.items() if key != 'offers'
    }
    if old != new:
        changed.append(new)
if mode == 'verified_variants_only':
    assert len(changed) == report['refreshed']
    for row in changed:
        offers = row['offers']
        assert len(offers) >= 2
        assert len({offer['merchant'].casefold() for offer in offers}) == len(offers)
        assert all(datetime.fromisoformat(offer['observed_at']) >= before for offer in offers)
