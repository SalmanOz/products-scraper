"""Read-only runner pilot of the production Vatan sitemap discovery path."""

import json
import os
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from tempfile import gettempdir

import requests
from bs4 import BeautifulSoup

from audit_vatan_source import PROFILES
from vatan_sitemap_source import (
    MAX_SITEMAP_BYTES, MAX_SITEMAP_URLS, SITEMAP_URL, SOLVER_URL,
    VatanSitemapSource,
)


OUT = Path(os.environ.get('VATAN_SITEMAP_AUDIT_OUTPUT_DIR', gettempdir()))
MAX_DIAGNOSTIC_SOLVER_BYTES = 32 * 1024 * 1024
_NS = '{http://www.sitemaps.org/schemas/sitemap/0.9}'


def diagnose_solver_response(post=None):
    """Measure one bounded retry; never retain its body, cookies or headers."""
    post = post or requests.post
    result = {'status': 'unavailable', 'solver_http_status': None,
              'declared_content_length': None, 'solver_bytes': None,
              'response_kind': None, 'response_bytes': None}
    try:
        response = post(SOLVER_URL, json={
            'cmd': 'request.get', 'url': SITEMAP_URL, 'maxTimeout': 45000,
        }, timeout=60, stream=True)
        try:
            result['solver_http_status'] = response.status_code
            length = str(response.headers.get('Content-Length') or '')
            result['declared_content_length'] = int(length) if length.isdigit() else None
            if response.status_code != 200:
                return {**result, 'status': 'solver_http_error'}
            data = bytearray()
            for chunk in response.iter_content(chunk_size=64 * 1024):
                if len(chunk) > 64 * 1024 or len(data) + len(chunk) > MAX_DIAGNOSTIC_SOLVER_BYTES:
                    return {**result, 'status': 'solver_response_too_large',
                            'solver_bytes_at_abort': len(data) + len(chunk)}
                data.extend(chunk)
            result['solver_bytes'] = len(data)
        finally:
            response.close()
        payload = json.loads(data)
        solution = payload.get('solution') if isinstance(payload, dict) else None
        if not isinstance(payload, dict) or not isinstance(solution, dict):
            return {**result, 'status': 'invalid_solver_json'}
        result['solution_status'] = solution.get('status') if isinstance(solution.get('status'), int) else None
        result['url_matches'] = solution.get('url') == SITEMAP_URL
        body = solution.get('response')
        if not isinstance(body, str):
            return {**result, 'status': 'missing_response'}
        encoded = body.encode('utf-8')
        result['response_bytes'] = len(encoded)
        prefix = encoded.lstrip()[:128].lower()
        result['response_kind'] = ('raw_xml' if prefix.startswith((b'<?xml', b'<urlset'))
                                   else 'html' if prefix.startswith((b'<!doctype html', b'<html'))
                                   else 'other')
        if result['response_kind'] == 'html':
            soup = BeautifulSoup(body, 'html.parser')
            result['browser_xml_structure'] = {
                'source_containers': len(soup.select('#webkit-xml-viewer-source-xml')),
                'urlset_nodes': len(soup.find_all('urlset')),
                'xml_pre_blocks': sum(node.get_text().lstrip().startswith(('<?xml', '<urlset'))
                                      for node in soup.select('body > pre')),
                'sitemap_namespace_present': 'http://www.sitemaps.org/schemas/sitemap/0.9' in body,
            }
        if (result['response_kind'] != 'raw_xml' or len(encoded) > MAX_SITEMAP_BYTES
                or b'<!DOCTYPE' in encoded.upper() or b'<!ENTITY' in encoded.upper()):
            return {**result, 'status': 'measured'}
        root = ET.fromstring(encoded)
        result['structural_urlset'] = root.tag == _NS + 'urlset' and len(root) <= MAX_SITEMAP_URLS
        result['url_count'] = len(root) if result['structural_urlset'] else None
        return {**result, 'status': 'measured'}
    except (requests.RequestException, ValueError, TypeError, UnicodeError, ET.ParseError):
        return {**result, 'status': 'probe_error'}


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
    probe = report.get('solver_diagnostic')
    if probe:
        lines.append(f"Solver diagnostic: {probe['status']}; declared {probe['declared_content_length']} B; "
                     f"read {probe['solver_bytes'] or probe.get('solver_bytes_at_abort')} B; "
                     f"response {probe['response_kind']} {probe['response_bytes']} B; "
                     f"structural URL set {probe.get('structural_urlset')}.")
    lines.append('No PDP price, catalog, ingestion, schema, R2, or IndexNow writes.')
    return '\n'.join(lines) + '\n'


def main():
    source = VatanSitemapSource()
    report = run_audit(source)
    # The read-only diagnostic keeps the exact sitemap URL and its own byte cap.
    if report['sitemap']['status'] in {'sitemap_solver_oversized', 'ParseError'}:
        report['solver_diagnostic'] = diagnose_solver_response()
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / 'vatan-sitemap-browser.json').write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    text = summary(report)
    (OUT / 'vatan-sitemap-browser.md').write_text(text)
    print(text, end='')
    return 0 if report['verified_discovery'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
