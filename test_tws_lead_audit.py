from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from tws_catalog_expansion import candidates_from_audit, stage
from tws_lead_audit import (KNOWN_MODELS, audit, collect_leads, discover_native_leads, identify, native_search_models,
                            search_native_retailer)


class EarbudLeadAuditTest(unittest.TestCase):
    def test_known_earbud_model_uses_exact_family(self):
        self.assertEqual(identify('Sony WF-1000XM6'), 'Sony WF-1000XM6')
        self.assertIsNone(identify('Sony WH-1000XM6'))
        self.assertIsNone(identify('Sony WF-1000XM6 Yenilenmiş'))
        self.assertEqual(identify('CMF by Nothing Buds 2a'), 'Nothing CMF Buds 2a')
        self.assertEqual(identify('Nothing EARA'), 'Nothing Ear (a)')
        self.assertIsNone(identify('Nothing Ear (3) Koruma Kılıfı'))
        self.assertEqual(identify('Anker Soundcore AeroClip Siyah'), 'Anker Soundcore AeroClip')
        self.assertIsNone(identify('Anker Soundcore AeroClip Special'))
        self.assertEqual(identify('Anker Soundcore P31i R60i'), 'Anker Soundcore R60i NC')
        self.assertIsNone(identify('Anker Soundcore R50i NC R60i ile Uyumlu Kılıf'))
        self.assertIsNone(identify('Anker Soundcore Sleep A30 Special'))
        self.assertIsNone(identify('Anker Soundcore Sleep A30 Kablosuz Kulaklık D1301S'))
        self.assertEqual(identify('Anker Soundcore Sleep A30 D1301'), 'Anker Soundcore Sleep A30')

    def test_earbud_budget_services_distinct_models_before_more_urls_for_one_model(self):
        host = 'www.n11.com'
        state = {'products': {
            '1': {'title': 'Sony WF-1000XM6', 'category': 'tws', 'retailerLeads': [
                f'https://{host}/urun/aaa-10001', f'https://{host}/urun/aaa-10002']},
            '2': {'title': 'Nothing Ear (a)', 'category': 'tws', 'retailerLeads': [
                f'https://{host}/urun/zzz-10003']},
        }}
        self.assertEqual([name for name, _ in collect_leads(state, maximum=2)[host]],
                         ['Sony WF-1000XM6', 'Nothing Ear (a)'])

    def test_native_search_uses_exact_earbud_cards_and_only_retains_url_hints(self):
        name = 'Sony WF-1000XM6'
        route = '/urun/sony-wf-1000xm6-siyah-12345678'
        url = 'https://www.n11.com' + route
        def card(path, title):
            return f'<a href="{path}"><h2 class="product-item-title">{title}</h2><span>1 TL</span></a>'
        html = card(route, name + ' Siyah') + card(route.replace('78', '79'), name + ' Kılıf')
        html += card('https://example.org' + route, name + ' Gümüş')
        document_reader = object()
        with patch('tws_lead_audit.catalog_document_reader', return_value=document_reader) as reader, \
                patch('tws_lead_audit.PriorityRetailerTransport') as transport:
            transport.return_value.read_n11_search.return_value = html, 'document_read', 200
            leads, report = search_native_retailer('www.n11.com', [name])
        reader.assert_called_once_with('earbud-discovery')
        transport.assert_called_once_with(maximum_per_host=1, browser_fallback=False, get=document_reader)
        self.assertEqual(leads, {name: [url]})
        self.assertEqual(report[0]['destinations'], 1)
        self.assertEqual(report[0]['collection'], 'site')
        state = {'products': {}, 'earbudNativeLeads': leads}
        self.assertEqual(collect_leads(state), {'www.n11.com': [(name, {'url': url})]})

    def test_native_search_rotates_bounded_model_gaps(self):
        now = datetime.now(timezone.utc)
        name, other, *remaining = sorted(KNOWN_MODELS)
        host = 'www.n11.com'
        covered = {'models': [{'name': name, 'gtin': '4548736171824',
                              'offers': [{'platform': 'n11'}, {'platform': 'Trendyol'}]}]}
        state = {'earbudNativeSearchAudits': {host: {other: now.isoformat()}}}
        self.assertEqual(native_search_models(state, covered, (), now, host), remaining[:4])
        self.assertEqual(native_search_models(state, covered, [name, other, 'Not a model', name], now, host), [name, other])
        state['earbudLeadObservations'] = {'https://www.n11.com/urun/earbuds-12345678':
            {'name': name, 'gtin13': '4548736171770', 'checkedAt': now.isoformat()}}
        self.assertEqual(native_search_models(state, covered, (), now, host), [name, *remaining[:3]])

    def test_denial_in_one_store_does_not_mark_its_model_as_searched(self):
        name = 'Sony WF-1000XM6'
        now = datetime.now(timezone.utc)
        state = {}

        def search(host, models):
            status = 403 if host == 'www.n11.com' else 200
            return {}, [{'model': model, 'httpStatus': status} for model in models]

        with patch('tws_lead_audit.search_native_retailer', side_effect=search):
            discover_native_leads(state, {}, [name], now)
        self.assertEqual(state['earbudNativeSearchAudits'], {
            'www.hepsiburada.com': {name: now.isoformat()}, 'www.n11.com': {},
        })

    def test_new_product_stages_all_fresh_independent_gtins_from_audit(self):
        name = 'Nothing Ear (a)'
        gtins = ['4548736171770', '4548736171824']
        offers = []
        for i, gtin in enumerate(gtins):
            for platform, domain, route in (
                ('n11', 'www.n11.com', f'/urun/nothing-ear-a-{10001+i}'),
                ('Trendyol', 'www.trendyol.com', f'/nothing/ear-a-p-{10001+i}'),
            ):
                offers.append({'gtin13': gtin, 'merchant': platform, 'platform': platform,
                               'url': f'https://{domain}{route}', 'price': 3000 + i,
                               'observed_at': 'current', '_legal_name': platform,
                               'source_title': name + (' Sarı' if i == 0 else ' Beyaz'),
                               '_image': 'https://example.test/product.webp'})
        prior = {'verified': [{'name': name, 'observations': offers, 'verifiedVariants': [
            {'gtin13': gtin, 'offers': [o for o in offers if o['gtin13'] == gtin]} for gtin in gtins]}]}
        targets = candidates_from_audit(prior)
        self.assertNotIn('price', targets[name][0])
        specs = {name: {'summary': 'Özet', 'highlights': ['Özellik'], 'specSource': 'https://example.test/spec',
                        'specs': [{'label': str(i), 'value': 'Değer'} for i in range(5)]}}

        def live(host, rows, document_reader=None):
            urls = {row['url'] for _, row in rows}
            return {name: [o for o in offers if o['url'] in urls]}, {}

        with TemporaryDirectory() as folder, patch('tws_catalog_expansion.reread', side_effect=live), \
                patch('tws_catalog_expansion._image', return_value=b'photo'):
            report = stage({'models': []}, {'models': []}, specs, targets, Path(folder))
            result = json.loads((Path(folder) / 'twsOfferSnapshot.json').read_text())
        self.assertEqual(report['qualifiedVariants'], 2)
        self.assertEqual([(row['gtin'], row['variant']) for row in result['models']],
                         [(gtins[0], 'Sarı'), (gtins[1], 'Beyaz')])
        self.assertEqual(set(result['sourceTargets']), {f'{name}/{gtin}' for gtin in gtins})

    def test_canonical_links_with_conflicting_owners_do_not_get_read(self):
        url = 'https://www.n11.com/urun/sony-wf-1000xm6-12345678'
        first = {'title': 'Sony WF-1000XM6', 'category': 'tws', 'retailerLeads': [url]}
        second = {**first, 'title': 'Sony WF-1000XM5'}
        self.assertEqual(collect_leads({'products': {'a': first, 'b': second}}), {})
        self.assertEqual(collect_leads({'products': {'a': first}}),
                         {'www.n11.com': [('Sony WF-1000XM6', {'url': url})]})

    def test_catalog_target_counts_only_if_reverified_on_the_same_barcode(self):
        name = 'Sony WF-1000XM6'
        gtin = '4548736171770'
        lead = 'https://www.n11.com/urun/sony-wf-1000xm6-12345678'
        target = 'https://www.mediamarkt.com.tr/tr/product/_sony-wf-1000xm6-siyah-1256567.html'
        snapshot = {'models': [{'name': name, 'gtin': gtin, 'offers': [{'url': target, 'price': 10}]}]}
        state = {'products': {'x': {'title': name, 'category': 'tws', 'retailerLeads': [lead]}}}

        def collect(host, rows, statuses, *, discover, document_reader=None):
            url = rows[0][1]['url']
            statuses[url] = 'verified'
            return {name: [{'url': url, 'merchant': host, 'platform': host, 'gtin13': gtin,
                            'price': 25499.0, '_legal_name': host}]}, {}

        with patch('tws_lead_audit.reread', side_effect=collect):
            variants = audit(state, snapshot=snapshot)['verified'][0]['verifiedVariants']
        self.assertEqual([r['gtin13'] for r in variants], [gtin])
        self.assertTrue(variants[0]['hasNewTarget'])
        self.assertEqual([o['price'] for o in variants[0]['offers']], [25499.0, 25499.0])

    def test_prior_verified_links_are_reread_without_reusing_old_prices(self):
        name, gtin = 'Sony WF-1000XM6', '4548736171770'
        n11 = 'https://www.n11.com/urun/sony-wf-1000xm6-12345678'
        mediamarkt = 'https://www.mediamarkt.com.tr/tr/product/_sony-wf-1000xm6-siyah-1256567.html'
        vatan = 'https://www.vatanbilgisayar.com/sony-wf-1000xm6-siyah.html'
        observed = {'name': name, 'gtin13': gtin, 'price': 1}
        now = datetime.now(timezone.utc)
        state = {
            'products': {'p': {'title': name, 'category': 'tws', 'retailerLeads': [n11]}},
            'earbudLeadObservations': {
                mediamarkt: {**observed, 'checkedAt': (now - timedelta(days=1)).isoformat()},
                vatan: {**observed, 'checkedAt': (now - timedelta(days=31)).isoformat()},
            },
        }

        def collect(host, rows, statuses, *, discover, document_reader=None):
            return ({name: [{'url': row['url'], 'merchant': host, 'platform': host, 'gtin13': gtin,
                             'price': 25000.0, '_legal_name': host} for _, row in rows]}, {})

        with patch('tws_lead_audit.reread', side_effect=collect) as read:
            result = audit(state)
        self.assertEqual({call.args[0] for call in read.call_args_list}, {'www.n11.com', 'www.mediamarkt.com.tr'})
        self.assertEqual([row['price'] for row in result['verified'][0]['verifiedPair']], [25000.0, 25000.0])
        self.assertNotIn('price', state['earbudLeadObservations'][mediamarkt])
        self.assertNotIn(vatan, state['earbudLeadObservations'])
