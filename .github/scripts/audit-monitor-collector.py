"""Check storefront responses using the monitor collector's approved URLs."""

import json
import os
from pathlib import Path
from urllib.parse import urlsplit

import requests


approved = set(json.loads(Path('site/frontend/src/data/monitorSourceTargets.json').read_bytes()))
targets = json.loads(Path('monitor_pilot_targets.json').read_bytes())
headers = {'Authorization': 'Bearer ' + os.environ['MONITOR_READER_OIDC']}
for host in ('www.vatanbilgisayar.com', 'www.mediamarkt.com.tr', 'www.incehesap.com'):
    urls = [row['url'] for row in targets if row['url'] in approved and urlsplit(row['url']).hostname == host]
    for url in urls[:7]:
        response = requests.post('https://teknoskor.com/api/ingestion/monitor-document',
                                 headers=headers, json={'url': url}, timeout=24, allow_redirects=False)
        data = response.json() if response.ok else {}
        status = data.get('source_status')
        print(json.dumps({'host': host, 'collector': response.status_code, 'source': status}), flush=True)
        if status in (403, 429):
            break
