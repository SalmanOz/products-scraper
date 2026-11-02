"""Offline iPhone 17 Pasaj one-off audit and writer boundaries."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import unittest

from iphone17_pasaj_manual_coverage import (
    CATALOG, MERCHANT, PRICE, TITLE, URL, audit, publish, summary, verified_offer,
)
from offer_ingestion_client import IngestionClientError, TARGET_INVALID_REASONS


NOW = datetime.now(timezone.utc)
VATAN = 'https://www.vatanbilgisayar.com/iphone-17-256-gb-akilli-telefon-beyaz.html'
MEDIAMARKT = ('https://www.mediamarkt.com.tr/tr/product/'
             '_apple-mg6k4tua-iphone-17-256gb-akilli-telefon-beyaz-1249222.html')
APPLE = 'https://www.apple.com/tr/shop/buy-iphone/iphone-17/6.3-in%C3%A7-ekran-256gb-siyah'
OTHER_OFFERS = [
    {'merchant_name': 'Vatan Bilgisayar', 'source_url': VATAN, 'price': 85799},
    {'merchant_name': 'MediaMarkt', 'source_url': MEDIAMARKT, 'price': 89999},
    {'merchant_name': 'Apple Store', 'source_url': APPLE, 'price': 99999},
]


class Source:
    def __init__(self):
        self.reads = []
        self.price = float(PRICE)
        self.reason = 'observed_exact_cash_offer'
        self.status = 200
        self.robots = 'allowed'

    def read_offer(self, url):
        self.reads.append(url)
        offer = {'merchant': MERCHANT, 'price': self.price, 'currency': 'TRY',
                 'availability': 'in_stock', 'source_title': TITLE, 'url': url,
                 'observed_at': datetime.now(timezone.utc).isoformat()}
        return offer, self.reason, self.status

    def report(self):
        return {'robots_status': self.robots, 'blocked': False,
                'pdp_requests': len(self.reads)}


class Client:
    def __init__(self, *, pasaj_stale=False):
        prior = datetime.now(timezone.utc) - timedelta(hours=80 if pasaj_stale else 8)
        self.targets = [
            {'merchant_name': 'Vatan Bilgisayar', 'source_url': VATAN,
             'seller_name': 'Vatan Bilgisayar',
             'checked_at': (NOW - timedelta(hours=1)).isoformat()},
            {'merchant_name': MERCHANT, 'seller_name': MERCHANT, 'source_url': URL,
             'checked_at': prior.isoformat()},
            {'merchant_name': 'MediaMarkt', 'source_url': MEDIAMARKT,
             'seller_name': 'MediaMarkt',
             'checked_at': (NOW - timedelta(hours=2)).isoformat()},
            {'merchant_name': 'Apple Store', 'source_url': APPLE,
             'seller_name': 'Apple Store',
             'checked_at': (NOW - timedelta(hours=3)).isoformat()},
        ]
        self.product = {'id': CATALOG['id'], 'slug': CATALOG['slug'],
                        'name': CATALOG['name'],
                        'attributes': {'ram_gb': 8, 'storage_gb': 256},
                        'offer_targets': deepcopy(self.targets),
                        'offer_targets_truncated': False}
        self.pasaj_stale = pasaj_stale
        self.identity_offers = deepcopy(OTHER_OFFERS)
        if not pasaj_stale:
            self.identity_offers.append({'merchant_name': MERCHANT,
                                         'source_url': URL, 'price': float(PRICE)})
        self.writes = []
        self.ack = {'committed': True, 'accepted': 1,
                    'stale_ignored': 0, 'variant_rejected': 0}
        self.snapshot = 'a' * 64
        self.after_write_change = False
        self.complete_failure = False
        self.invalid_count = 0
        self.duplicate_count = 0
        self.invalid_reasons = {key: 0 for key in TARGET_INVALID_REASONS}
        self.inspection_overrides = {}
        self.raw_overrides = {}
        self.raw_failure = False

    def get_published_products(self):
        return [deepcopy(self.product)]

    def get_public_price_priority(self):
        return {CATALOG['id']: (CATALOG['slug'], True)}

    def get_complete_offer_targets(self, _id):
        if self.complete_failure:
            raise IngestionClientError('Complete target source has invalid rows')
        return {'id': CATALOG['id'], 'slug': CATALOG['slug'],
                'name': CATALOG['name'], 'offer_targets': deepcopy(self.targets),
                'offer_targets_total': len(self.targets),
                'offer_targets_truncated': False,
                'offer_targets_snapshot': self.snapshot}

    def get_offer_target_inspection(self, _id):
        return {'id': CATALOG['id'], 'slug': CATALOG['slug'],
                'name': CATALOG['name'],
                'attributes': deepcopy(self.product['attributes']),
                'offer_targets': deepcopy(self.targets),
                'offer_targets_total': len(self.targets),
                'offer_targets_invalid_count': self.invalid_count,
                'offer_targets_duplicate_count': self.duplicate_count,
                'offer_targets_invalid_reasons': deepcopy(self.invalid_reasons),
                'offer_targets_truncated': bool(self.invalid_count or self.duplicate_count),
                'offer_targets_snapshot': self.snapshot,
                **deepcopy(self.inspection_overrides)}

    def get_raw_seller_collision_proof(self, product_id, seller, url):
        if self.raw_failure:
            raise IngestionClientError('Raw seller proof unavailable')
        assert (product_id, seller, url) == (CATALOG['id'], MERCHANT, URL)
        return {'purpose': 'read_only_raw_seller_collision_proof',
                'writes': False, 'product_id': product_id,
                'complete': True, 'blocked': False, 'reasons': [],
                'stored_rows_checked': len(self.targets) + self.invalid_count,
                'unknown_seller_rows': 0, 'invalid_row_identity_rows': 0,
                'invalid_url_rows': 0, 'proposed_seller_other_url_rows': 0,
                'exact_seller_url_rows': 1,
                'refresh_targets_total': len(self.targets),
                'refresh_targets_invalid_count': self.invalid_count,
                'refresh_targets_duplicate_count': self.duplicate_count,
                'refresh_targets_invalid_reasons': deepcopy(self.invalid_reasons),
                'refresh_targets_snapshot': self.snapshot,
                **deepcopy(self.raw_overrides)}

    def get_offer_identity_readiness(self):
        return {'evidenceVersion': 'product-offer-match-v1',
                'mode': 'shadow_audit_only',
                'auditedAt': datetime.now(timezone.utc).isoformat(),
                'productCoverage': [{
                    'slug': CATALOG['slug'],
                    'matchedMerchants': len(self.identity_offers),
                    'matchedLowestPrice': min(x['price'] for x in self.identity_offers),
                    'matchedOffers': deepcopy(self.identity_offers),
                }]}

    def ingest_offers(self, rows, *, require_all_accepted=False):
        self.writes.append((deepcopy(rows), require_all_accepted))
        if self.ack['accepted'] == 1:
            self.targets[1]['checked_at'] = rows[0]['checked_at']
            self.product['offer_targets'] = deepcopy(self.targets)
            if self.pasaj_stale:
                self.identity_offers.append({'merchant_name': MERCHANT,
                                             'source_url': URL, 'price': float(PRICE)})
                self.pasaj_stale = False
            if self.after_write_change:
                self.identity_offers[0]['price'] += 1
        return deepcopy(self.ack)


class Iphone17PasajManualTests(unittest.TestCase):
    @staticmethod
    def historical_row_client(reason='unsupported_host'):
        client = Client()
        client.complete_failure = True
        client.invalid_count = 1
        client.invalid_reasons[reason] = 1
        client.product['offer_targets_truncated'] = True
        return client

    def test_default_audit_reads_one_exact_pdp_and_four_seller_baseline(self):
        client, source = Client(), Source()
        report = audit(client=client, source=source)
        self.assertEqual(report['status'], 'passed', report['catalog_error'])
        self.assertIs(report['writes'], False)
        self.assertEqual(source.reads, [URL])
        self.assertEqual(report['catalog_evidence']['target_count'], 4)
        self.assertEqual(len(report['catalog_evidence']['other_offers']), 3)
        self.assertTrue(report['catalog_evidence']['pasaj_current'])
        self.assertEqual(client.writes, [])

    def test_safe_historical_row_uses_one_page_and_independent_raw_proof(self):
        for reason in ('unsupported_host', 'retailer_query_restricted'):
            with self.subTest(reason=reason):
                client = self.historical_row_client(reason)
                source = Source()
                report = audit(client=client, source=source)
                self.assertEqual(report['status'], 'passed', report['catalog_error'])
                proof = report['catalog_evidence']
                self.assertEqual(proof['target_mode'], 'single_page_raw_proven')
                self.assertEqual((proof['target_count'], proof['invalid_count']), (4, 1))
                self.assertEqual(proof['raw_seller_proof']['exact_seller_url_rows'], 1)
                self.assertEqual(client.writes, [])
                result = publish(report, client=client, source=source)
                self.assertEqual(result['accepted'], 1)
                self.assertEqual(len(client.writes), 1)

    def test_stale_other_seller_on_pasaj_platform_does_not_mask_exact_seller(self):
        client = self.historical_row_client()
        client.targets.append({
            'merchant_name': MERCHANT, 'seller_name': 'Turkcell',
            'source_url': URL + '?source=legacy',
            'checked_at': (NOW - timedelta(days=24)).isoformat(),
        })
        client.product['offer_targets'] = deepcopy(client.targets)
        source = Source()
        report = audit(client=client, source=source)
        self.assertEqual(report['status'], 'passed', report['catalog_error'])
        self.assertEqual(report['catalog_evidence']['target_count'], 5)
        self.assertEqual(report['catalog_evidence']['prior_pasaj_checked_at'],
                         client.targets[1]['checked_at'])
        self.assertEqual(source.reads, [URL])
        self.assertEqual(client.writes, [])

    def test_fallback_rejects_unsafe_or_incomplete_historical_rows(self):
        changes = (
            lambda c: c.invalid_reasons.update(
                unsupported_host=0, malformed_url=1),
            lambda c: setattr(c, 'duplicate_count', 1),
            lambda c: c.inspection_overrides.update(
                offer_targets_snapshot='b' * 64),
            lambda c: c.inspection_overrides.update(
                offer_targets_total=13),
            lambda c: c.inspection_overrides.update(
                offer_targets=[*c.targets, c.targets[0]]),
            lambda c: c.raw_overrides.update(unknown_seller_rows=1),
            lambda c: c.raw_overrides.update(invalid_row_identity_rows=1),
            lambda c: c.raw_overrides.update(invalid_url_rows=1),
            lambda c: c.raw_overrides.update(exact_seller_url_rows=0),
            lambda c: c.raw_overrides.update(proposed_seller_other_url_rows=1),
            lambda c: c.raw_overrides.update(refresh_targets_snapshot='b' * 64),
            lambda c: c.raw_overrides.update(stored_rows_checked=6),
            lambda c: setattr(c, 'raw_failure', True),
        )
        for change in changes:
            with self.subTest(change=str(change)):
                client = self.historical_row_client()
                source = Source()
                change(client)
                report = audit(client=client, source=source)
                self.assertEqual(report['status'], 'failed')
                self.assertEqual(source.reads, [])
                self.assertEqual(client.writes, [])

    def test_fallback_rejects_pasaj_or_same_url_collision(self):
        for change in ('pasaj_other_url', 'other_seller_same_url'):
            with self.subTest(change=change):
                client = self.historical_row_client()
                if change == 'pasaj_other_url':
                    client.targets.append({'merchant_name': MERCHANT,
                                           'seller_name': MERCHANT,
                                           'source_url': URL + '?other=1',
                                           'checked_at': NOW.isoformat()})
                else:
                    client.targets.append({'merchant_name': MERCHANT,
                                           'seller_name': 'Another Store',
                                           'source_url': URL,
                                           'checked_at': NOW.isoformat()})
                client.product['offer_targets'] = deepcopy(client.targets)
                report = audit(client=client, source=Source())
                self.assertEqual(report['status'], 'failed')
                self.assertEqual(client.writes, [])

    def test_same_price_new_observation_refreshes_only_existing_pasaj_row(self):
        client, source = Client(), Source()
        report = audit(client=client, source=source)
        before = deepcopy(client.identity_offers)
        result = publish(report, client=client, source=source)
        self.assertEqual((result['submitted'], result['accepted']), (1, 1))
        self.assertEqual(source.reads, [URL, URL])
        self.assertEqual(len(client.writes), 1)
        rows, atomic = client.writes[0]
        self.assertTrue(atomic)
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]['product_id'], rows[0]['seller'],
                          rows[0]['source_url'], rows[0]['price']),
                         (235, MERCHANT, URL, 86999.0))
        self.assertEqual(client.identity_offers, before)

    def test_stale_pasaj_row_can_reenter_without_changing_three_other_sellers(self):
        client, source = Client(pasaj_stale=True), Source()
        report = audit(client=client, source=source)
        self.assertEqual(report['status'], 'passed', report['catalog_error'])
        self.assertFalse(report['catalog_evidence']['pasaj_current'])
        before_others = deepcopy(client.identity_offers)
        publish(report, client=client, source=source)
        self.assertEqual(client.identity_offers[:3], before_others)
        self.assertEqual(len(client.identity_offers), 4)

    def test_seller_and_source_url_collisions_fail_before_source_fetch(self):
        for change in ('other_url', 'other_seller'):
            with self.subTest(change=change):
                client, source = Client(), Source()
                if change == 'other_url':
                    client.targets[1]['source_url'] = URL + '?variant=other'
                else:
                    client.targets[1]['seller_name'] = 'Another Store'
                client.product['offer_targets'] = deepcopy(client.targets)
                report = audit(client=client, source=source)
                self.assertEqual(report['status'], 'failed')
                self.assertEqual(report['catalog_error'],
                                 'pasaj_seller_or_source_url_collision')
                self.assertEqual(source.reads, [])
                self.assertEqual(client.writes, [])

    def test_selected_cash_price_drift_and_stale_ack_fail_closed(self):
        client, source = Client(), Source()
        source.price = 87999
        report = audit(client=client, source=source)
        self.assertEqual(report['status'], 'failed')
        self.assertEqual(client.writes, [])
        source.price = float(PRICE)
        report = audit(client=client, source=source)
        self.assertIsNotNone(verified_offer(report))
        client.ack['accepted'] = 0
        client.ack['stale_ignored'] = 1
        with self.assertRaisesRegex(RuntimeError, 'not_accepted'):
            publish(report, client=client, source=source)
        self.assertEqual(len(client.writes), 1)

    def test_other_seller_price_change_blocks_before_write(self):
        client, source = Client(), Source()
        report = audit(client=client, source=source)
        client.identity_offers[0]['price'] = 86000
        with self.assertRaisesRegex(ValueError, 'catalog_or_prior_seller_snapshot_changed'):
            publish(report, client=client, source=source)
        self.assertEqual(client.writes, [])

    def test_source_price_drift_and_future_stored_time_block_before_write(self):
        client, source = Client(), Source()
        report = audit(client=client, source=source)
        source.price = 87999
        with self.assertRaisesRegex(ValueError, 'live_offer_changed'):
            publish(report, client=client, source=source)
        self.assertEqual(client.writes, [])

        client, source = Client(), Source()
        client.targets[1]['checked_at'] = (
            datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        client.product['offer_targets'] = deepcopy(client.targets)
        report = audit(client=client, source=source)
        self.assertEqual(report['status'], 'failed')
        self.assertEqual(source.reads, [])

    def test_post_write_readback_detects_other_seller_change(self):
        client, source = Client(), Source()
        report = audit(client=client, source=source)
        client.after_write_change = True
        with self.assertRaisesRegex(RuntimeError, 'readback_changed'):
            publish(report, client=client, source=source)
        self.assertEqual(len(client.writes), 1)

    def test_summary_requires_exact_publication_and_passed_audit(self):
        client, source = Client(), Source()
        report = audit(client=client, source=source)
        publication = publish(report, client=client, source=source)
        self.assertIn('dört satıcı readback doğrulandı',
                      summary(report, publication, publish_requested=True))
        bad_publications = (
            {**publication, 'status': 'failed'},
            {**publication, 'submitted': 0},
            {**publication, 'submitted': True},
            {**publication, 'accepted': 0},
            {**publication, 'accepted': True},
            {**publication, 'slug': 'another-product'},
            {**publication, 'seller': 'Another seller'},
            {**publication, 'source_url': 'https://example.com/other'},
            {**publication, 'price': 87000},
            {**publication, 'price': True},
            {**publication, 'readback': 'missing'},
        )
        for candidate in bad_publications:
            with self.subTest(candidate=candidate):
                rendered = summary(report, candidate, publish_requested=True)
                self.assertIn('Yayın sonucu belirsiz', rendered)
                self.assertNotIn('readback doğrulandı', rendered)
        for changed_report in (
            {**report, 'status': 'failed'},
            {**report, 'catalog_evidence': None},
            {**report, 'writes': True},
        ):
            with self.subTest(report=changed_report):
                rendered = summary(changed_report, publication,
                                   publish_requested=True)
                self.assertNotIn('readback doğrulandı', rendered)


if __name__ == '__main__':
    unittest.main()
