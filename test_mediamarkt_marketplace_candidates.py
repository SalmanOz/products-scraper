"""Candidate queue is bounded, read-only, and never treats URLs as prices."""

from datetime import datetime, timezone
import json
import unittest
from unittest.mock import patch

from audit_mediamarkt_marketplace_candidates import (
    BASE_CATEGORY_URL, CATEGORY_HINT_URLS, MAX_MODEL_FACETS, MAX_PROBES,
    _audit_variant_hint, _browser_category, _catalog_model_facets,
    _read_selected, run_audit, summary,
)
from audit_mediamarkt_unpriced_coverage import CATALOG_URL


REDMI = ('https://www.mediamarkt.com.tr/tr/product/'
         '_xiaomi-redmi-note-15-pro-5g-8-gb-256-gb-akilli-telefon-buz-mavisi-169130258.html')
POCO = ('https://www.mediamarkt.com.tr/tr/product/'
        '_poco-x6-pro-8-gb-256-gb-akilli-telefon-siyah-142988315.html')
A36 = ('https://www.mediamarkt.com.tr/tr/product/'
       '_samsung-galaxy-a36-5g-128-gb-akilli-telefon-siyah-123456789.html')


def product(id, slug, name, *, price=None, radio='5g'):
    return {'id': id, 'slug': slug, 'name': name,
            'attributes': {'ram_gb': 8, 'storage_gb': 256,
                           'Connectivity': {'5G': 'var' if radio == '5g' else 'yok'}},
            'fresh_lowest_price': price, 'fresh_offer_count': int(price is not None)}


def selected(url, *, radio='5g'):
    return {'platform': 'MediaMarkt', 'seller_name': 'CepHane Teknoloji',
            'seller_id': '11289', 'price': 30499.0, 'currency': 'TRY',
            'availability': 'in_stock', 'condition': 'new',
            'source_title': ('XIAOMI Redmi Note 15 Pro 5G 8 GB 256 GB Akıllı Telefon Buz Mavisi'
                             if url == REDMI else 'POCO X6 Pro 8 GB 256 GB Akıllı Telefon Siyah'),
            'sku': '169130258' if url == REDMI else '142988315',
            'gtin13': '6932554491543' if url == REDMI else '6941812757994',
            'offer_id': '12345678', 'url': url, 'ram_gb': 8,
            'storage_gb': 256, 'network_generation': radio,
            'observed_at': datetime.now(timezone.utc).isoformat()}


class Response:
    def __init__(self, rows):
        self.data = json.dumps(rows).encode()
        self.status_code = 200
        self.url = CATALOG_URL
        self.headers = {'Content-Length': str(len(self.data))}

    def iter_content(self, chunk_size):
        for start in range(0, len(self.data), chunk_size):
            yield self.data[start:start + chunk_size]

    def close(self):
        pass


class SolverResponse:
    def __init__(self, url, html, *, final_url=None):
        self.status_code = 200
        self.data = json.dumps({'status': 'ok', 'solution': {
            'url': final_url or url, 'status': 200, 'response': html}}).encode()

    def iter_content(self, chunk_size):
        yield self.data

    def close(self):
        pass


class Source:
    def __init__(self, mapping=None, *, sitemap='ready', direct_reason='ready',
                 browser_url=None, browser_reason='ready', category_pages=None,
                 category_direct_reason='ready', category_final_url=None,
                 robots='ready'):
        self.mapping = mapping or {}
        self.category_pages = category_pages or {}
        self.category_direct_reason = category_direct_reason
        self.category_final_url = category_final_url
        self.sitemap = sitemap
        self.robots = robots
        self.direct_reason = direct_reason
        self.browser_url = browser_url
        self.browser_reason = browser_reason
        self._rules = self if robots == 'ready' else None
        self.direct = []
        self.browser = []
        self.category_browser = []

    def discover(self, name, specs):
        return ([{'source_url': url} for url in self.mapping.get(name, [])]
                if self.sitemap == 'ready' else [])

    def can_fetch(self, bot, url):
        return True

    def _direct(self, url, limit, accept):
        self.direct.append(url)
        reason = (self.category_direct_reason if url.startswith(BASE_CATEGORY_URL)
                  else self.direct_reason)
        body = self.category_pages.get(url, '<html></html>')
        return (body.encode() if reason == 'ready' else None,
                reason, 200 if reason == 'ready' else 403)

    def _pace(self, *, browser=False):
        assert browser

    def _post(self, url, *, json, timeout, stream):
        category_url = json['url']
        self.category_browser.append(category_url)
        return SolverResponse(category_url, self.category_pages.get(category_url, ''),
                              final_url=self.category_final_url)

    def _browser_get(self, url):
        self.browser.append(url)
        return (b'<html></html>', self.browser_url or url, self.browser_reason)

    def report(self):
        return {'robots_status': self.robots, 'sitemap_status': self.sitemap,
                'direct_requests': len(self.direct),
                'browser_requests': len(self.browser) + len(self.category_browser)}


class MarketplaceCandidateAuditTests(unittest.TestCase):
    def catalog_get(self, rows):
        return lambda *_args, **_kwargs: Response(rows)

    def test_unpriced_source_verified_and_missing_radio_are_separate_rows(self):
        rows = [product(238, 'xiaomi-redmi-note-15-pro', 'Xiaomi Redmi Note 15 Pro'),
                product(183, 'poco-x6-pro', 'POCO X6 Pro'),
                product(77, 'priced-phone', 'Priced Phone', price=40000)]
        source = Source({'Xiaomi Redmi Note 15 Pro': [REDMI],
                         'POCO X6 Pro': [POCO], 'Priced Phone': [REDMI]})
        with patch('audit_mediamarkt_marketplace_candidates.selected_marketplace_offer',
                   side_effect=lambda body, url: selected(url, radio='5g' if url == REDMI else None)):
            report = run_audit(catalog_get=self.catalog_get(rows), source=source)
        self.assertEqual(report['status'], 'completed')
        self.assertFalse(report['writes'])
        self.assertEqual(report['unpriced_products'], 2)
        self.assertEqual(report['probed'], 2)
        self.assertEqual(report['source_verified'], 1)
        self.assertEqual({row['review_status'] for row in report['candidates']},
                         {'source_verified_pending_private_target_review',
                          'catalog_radio_unproven_in_selected_pdp'})
        self.assertEqual({row['selected_offer']['gtin13'] for row in report['candidates']},
                         {'6932554491543', '6941812757994'})
        self.assertIn('no price, score or catalog writes', summary(report))

    def test_missing_selected_radio_never_counts_as_source_verified(self):
        row = product(238, 'xiaomi-redmi-note-15-pro', 'Xiaomi Redmi Note 15 Pro')
        row['attributes'] = {'ram_gb': 8, 'storage_gb': 256}
        source = Source({'Xiaomi Redmi Note 15 Pro': [REDMI]})
        with patch('audit_mediamarkt_marketplace_candidates.selected_marketplace_offer',
                   return_value=selected(REDMI, radio=None)):
            report = run_audit(catalog_get=self.catalog_get([row]), source=source)
        self.assertEqual(report['probed'], 1)
        self.assertEqual(report['source_verified'], 0)
        self.assertEqual(report['candidates'][0]['review_status'], 'selected_radio_unproven')
        self.assertIsNone(report['candidates'][0]['selected_offer']['network_generation'])

    def test_ambiguous_catalog_mapping_skips_pdp_and_priced_rows(self):
        rows = [product(1, 'redmi-a', 'Redmi A'), product(2, 'redmi-b', 'Redmi B'),
                product(3, 'priced', 'Priced', price=10000)]
        source = Source({'Redmi A': [REDMI], 'Redmi B': [REDMI], 'Priced': [POCO]})
        report = run_audit(catalog_get=self.catalog_get(rows), source=source)
        self.assertEqual(report['status'], 'completed')
        self.assertEqual(report['candidate_urls'], 0)
        self.assertEqual(len(report['ambiguous_urls']), 1)
        self.assertEqual(report['probed'], 0)
        self.assertEqual(source.direct, [*CATEGORY_HINT_URLS, BASE_CATEGORY_URL])

    def test_offset_and_fixed_budget_never_probe_more_than_twelve(self):
        rows = [product(i, f'phone-{i}', f'Phone {i}') for i in range(20)]
        urls = [REDMI.rsplit('-', 1)[0] + f'-{100000000 + i}.html'
                for i in range(20)]
        source = Source({f'Phone {i}': [urls[i]] for i in range(20)})
        # A syntactically valid public URL is still only a hint, not a price.
        with patch('audit_mediamarkt_marketplace_candidates.selected_marketplace_offer',
                   return_value=None):
            report = run_audit(catalog_get=self.catalog_get(rows), source=source, offset=3)
        self.assertEqual(report['candidate_urls'], 20)
        self.assertEqual(report['probed'], MAX_PROBES)
        self.assertEqual(len(source.direct), MAX_PROBES + len(CATEGORY_HINT_URLS) + 1)
        self.assertEqual(source.direct[:2], list(CATEGORY_HINT_URLS))
        self.assertEqual(report['source_verified'], 0)

    def test_two_category_pages_recover_exact_urls_missing_from_sitemap(self):
        rows = [product(238, 'xiaomi-redmi-note-15-pro', 'Xiaomi Redmi Note 15 Pro'),
                product(183, 'poco-x6-pro', 'POCO X6 Pro')]
        source = Source(category_pages={
            CATEGORY_HINT_URLS[0]: (
                '<a href="' + POCO + '">POCO</a>'
                '<a href="https://elsewhere.example/tr/product/_poco-x6-pro-akilli-telefon-123456.html">other</a>'),
            CATEGORY_HINT_URLS[1]: '<a href="' + REDMI + '">Redmi</a>',
        })
        with patch('audit_mediamarkt_marketplace_candidates.selected_marketplace_offer',
                   side_effect=lambda body, url: selected(url)):
            report = run_audit(catalog_get=self.catalog_get(rows), source=source)
        self.assertEqual(report['category_hint_urls'], 2)
        self.assertEqual(report['candidate_urls'], 2)
        self.assertEqual(report['probed'], 2)
        self.assertEqual(report['source_verified'], 2)
        self.assertEqual({r['source_url'] for r in report['candidates']}, {REDMI, POCO})
        self.assertEqual(source.direct[:2], list(CATEGORY_HINT_URLS))

    def test_category_403_gets_one_exact_browser_read_and_rejects_redirect(self):
        rows = [product(238, 'xiaomi-redmi-note-15-pro', 'Xiaomi Redmi Note 15 Pro'),
                product(183, 'poco-x6-pro', 'POCO X6 Pro')]
        pages = {CATEGORY_HINT_URLS[0]: '<a href="' + POCO + '">POCO</a>',
                 CATEGORY_HINT_URLS[1]: '<a href="' + REDMI + '">Redmi</a>'}
        source = Source(category_pages=pages, category_direct_reason='http_403')
        with patch('audit_mediamarkt_marketplace_candidates.selected_marketplace_offer',
                   side_effect=lambda body, url: selected(url)):
            report = run_audit(catalog_get=self.catalog_get(rows), source=source)
        self.assertEqual(source.category_browser, [*CATEGORY_HINT_URLS, BASE_CATEGORY_URL])
        self.assertEqual(report['probed'], 2)
        self.assertEqual(report['source_verified'], 2)
        self.assertEqual(set(report['category_status'].values()), {'browser_ready'})
        redirected = Source(category_pages=pages, category_final_url=POCO)
        body, reason = _browser_category(redirected, CATEGORY_HINT_URLS[0])
        self.assertIsNone(body)
        self.assertEqual(reason, 'changed_or_unavailable_category')

    def test_exact_403_browser_retry_and_redirect_rejection(self):
        source = Source(direct_reason='http_403')
        with patch('audit_mediamarkt_marketplace_candidates.selected_marketplace_offer',
                   return_value=selected(REDMI)):
            offer, attempt = _read_selected(source, REDMI)
        self.assertIsNotNone(offer)
        self.assertEqual(attempt['reason'], 'selected_marketplace_offer')
        self.assertEqual(source.browser, [REDMI])
        changed = Source(direct_reason='http_403', browser_url=POCO)
        offer, attempt = _read_selected(changed, REDMI)
        self.assertIsNone(offer)
        self.assertEqual(attempt['reason'], 'changed_selected_url')

    def test_sitemap_failure_allows_independent_category_evidence(self):
        rows = [product(238, 'xiaomi-redmi-note-15-pro', 'Xiaomi Redmi Note 15 Pro')]
        source = Source(sitemap='blocked', category_pages={
            CATEGORY_HINT_URLS[1]: '<a href="' + REDMI + '">Redmi</a>'})
        with patch('audit_mediamarkt_marketplace_candidates.selected_marketplace_offer',
                   return_value=selected(REDMI)):
            report = run_audit(catalog_get=self.catalog_get(rows), source=source)
        self.assertEqual(report['status'], 'completed')
        self.assertEqual(report['source_verified'], 1)
        self.assertIn('sitemap blocked', summary(report))

    def test_no_robots_or_discovery_source_fails_without_pdp(self):
        rows = [product(238, 'xiaomi-redmi-note-15-pro', 'Xiaomi Redmi Note 15 Pro')]
        source = Source(sitemap='blocked', category_direct_reason='http_500')
        report = run_audit(catalog_get=self.catalog_get(rows), source=source)
        self.assertEqual(report['status'], 'failed')
        self.assertEqual(report['error'], 'mediamarkt_discovery_unavailable')
        self.assertEqual(source.direct, [*CATEGORY_HINT_URLS, BASE_CATEGORY_URL])
        self.assertEqual(report['probed'], 0)
    def test_public_model_facet_pages_are_bounded_and_only_selected_pdp_proves_ram(self):
        rows = [product(277, 'samsung-galaxy-a36-5g', 'Samsung Galaxy A36 5G')]
        rows[0]['attributes']['ram_gb'] = 6
        rows[0]['attributes']['storage_gb'] = 128
        facet_url = BASE_CATEGORY_URL + '?model=Galaxy%20A36'
        root = ('<details data-test="mms-facet-multiselect-model">'
                '<label data-test="checkbox"><input type="checkbox" id="Galaxy_A36-model" '
                'name="Galaxy_A36">Galaxy A36 (4 Sonuçlar)</label>'
                '<label data-test="checkbox"><input type="checkbox" id="Galaxy_A17-model" '
                'name="Galaxy_A17">Galaxy A17 (9 Sonuçlar)</label>'
                '</details>')
        source = Source(category_pages={BASE_CATEGORY_URL: root,
                                        facet_url: '<a href="' + A36 + '">A36</a>'})
        offer = {**selected(REDMI), 'url': A36,
                 'source_title': 'SAMSUNG Galaxy A36 5G 6 GB 128 GB Akıllı Telefon Siyah',
                 'sku': '123456789', 'ram_gb': 6, 'storage_gb': 128}
        with patch('audit_mediamarkt_marketplace_candidates.selected_marketplace_offer',
                   return_value=offer):
            report = run_audit(catalog_get=self.catalog_get(rows), source=source)
        self.assertEqual(report['status'], 'completed')
        self.assertEqual(report['model_facets'], 1)
        self.assertEqual(report['selected_model_facets'], ['Galaxy A36'])
        self.assertEqual(report['candidate_urls'], 1)
        self.assertEqual(report['source_verified'], 1)
        self.assertEqual(report['category_status'][facet_url], 'ready')
        self.assertLessEqual(len(report['selected_model_facets']), MAX_MODEL_FACETS)
        with patch('audit_mediamarkt_marketplace_candidates.selected_marketplace_offer',
                   return_value={**offer, 'ram_gb': 8}):
            wrong_ram = run_audit(catalog_get=self.catalog_get(rows), source=source)
        self.assertEqual(wrong_ram['source_verified'], 0)
        self.assertEqual(wrong_ram['candidates'][0]['review_status'], 'catalog_variant_mismatch')
        later = run_audit(catalog_get=self.catalog_get(rows), source=source, model_offset=1)
        self.assertEqual(later['selected_model_facets'], [])
        self.assertEqual(later['probed'], 0)

    def test_four_live_exact_variants_remain_hints_until_selected_radio_and_gtin_pass(self):
        redmi = product(238, 'xiaomi-redmi-note-15-pro', 'Xiaomi Redmi Note 15 Pro')
        poco = product(183, 'poco-x6-pro', 'POCO X6 Pro')
        four = [
            (POCO, poco, 'POCO X6 Pro 8 GB 256 GB Akıllı Telefon Siyah'),
            (REDMI, redmi, 'XIAOMI Redmi Note 15 Pro 5G 8 GB 256 GB Akıllı Telefon Buz Mavisi'),
            (REDMI.replace('buz-mavisi-169130258', 'siyah-169130256'), redmi,
             'XIAOMI Redmi Note 15 Pro 5G 8 GB 256 GB Akıllı Telefon Siyah'),
            (REDMI.replace('buz-mavisi-169130258', 'titanyum-gri-169130275'), redmi,
             'XIAOMI Redmi Note 15 Pro 5G 8 GB 256 GB Akıllı Telefon Titanyum Gri'),
        ]
        from audit_mediamarkt_marketplace_candidates import _review
        from audit_mediamarkt_unpriced_coverage import _specs
        for url, row, title in four:
            specs = _specs(row)
            self.assertTrue(_audit_variant_hint(url, row, specs), url)
            evidence = {**selected(REDMI), 'url': url, 'source_title': title}
            self.assertEqual(_review(evidence, row, specs),
                             'source_verified_pending_private_target_review')
            self.assertEqual(_review({**evidence, 'network_generation': None}, row, specs),
                             'catalog_radio_unproven_in_selected_pdp')
            self.assertEqual(_review({**evidence, 'gtin13': None}, row, specs),
                             'selected_gtin_missing')

    def test_parent_model_facet_can_hint_child_without_numeric_model_conflation(self):
        from audit_mediamarkt_unpriced_coverage import _specs
        redmi = product(251, 'xiaomi-redmi-note-14-pro', 'Xiaomi Redmi Note 14 Pro')
        a56 = product(242, 'samsung-galaxy-a56', 'Samsung Galaxy A56')
        root = ('<details data-test="mms-facet-multiselect-model">'
                '<label data-test="checkbox"><input type="checkbox" '
                'id="XIAOMI_Redmi_Note_14-model" name="XIAOMI_Redmi_Note_14">'
                'XIAOMI Redmi Note 14 (5 Sonuçlar)</label>'
                '<label data-test="checkbox"><input type="checkbox" '
                'id="Galaxy_A5-model" name="Galaxy_A5">Galaxy A5 (5 Sonuçlar)</label>'
                '<label data-test="checkbox"><input type="checkbox" '
                'id="Galaxy_A56-model" name="Wrong_name">Galaxy A56 (6 Sonuçlar)</label>'
                '</details>')
        self.assertEqual(_catalog_model_facets(root.encode(),
            [(redmi, _specs(redmi)), (a56, _specs(a56))]), ['XIAOMI Redmi Note 14'])

    def test_model_facet_offset_reads_at_most_five_pages_and_skips_fixed_repeat(self):
        rows = [product(number, f'galaxy-a{number}', f'Samsung Galaxy A{number}')
                for number in range(30, 37)]
        root = '<details data-test="mms-facet-multiselect-model">' + ''.join(
            f'<label data-test="checkbox"><input type="checkbox" '
            f'id="Galaxy_A{number}-model" name="Galaxy_A{number}">'
            f'Galaxy A{number} (4 Sonuçlar)</label>' for number in range(30, 37)
        ) + '</details>'
        source = Source(category_pages={BASE_CATEGORY_URL: root})
        report = run_audit(catalog_get=self.catalog_get(rows), source=source,
                           model_offset=1)
        self.assertEqual(report['status'], 'completed')
        self.assertEqual(report['model_facets'], 7)
        self.assertEqual(report['selected_model_facets'],
                         [f'Galaxy A{number}' for number in range(31, 36)])
        self.assertEqual(source.direct, [BASE_CATEGORY_URL] + [
            BASE_CATEGORY_URL + f'?model=Galaxy%20A{number}'
            for number in range(31, 36)])
        self.assertEqual(report['probed'], 0)


if __name__ == '__main__':
    unittest.main()
