from unittest import TestCase
from unittest.mock import patch

from tws_refresh import refresh


class TwsRefreshTest(TestCase):
    def test_unverified_offer_is_withdrawn_and_rechecked_on_next_run(self):
        name = 'Anker Soundcore AeroFit 2'
        gtin = '0194644298876'
        vatan = 'https://www.vatanbilgisayar.com/anker-soundcore-aerofit-2-kulak-ici-bluetooth-kulaklik-siyah.html'
        teknosa = 'https://www.teknosa.com/anker-soundcore-aerofit-2-a3874-siyah-kulaklik-p-100000063496'
        prior = [
            {'url': vatan, 'merchant': 'Vatan Bilgisayar', 'platform': 'Vatan Bilgisayar', 'price': 6999.0},
            {'url': teknosa, 'merchant': 'Teknosa', 'platform': 'Teknosa', 'price': 5799.0},
        ]
        catalog = {'models': [{'name': name, 'requiresIndependentPair': True}]}
        snapshot = {'checked_at': 'old', 'models': [{'name': name, 'gtin': gtin, 'offers': prior}]}

        def collect(host, rows, statuses):
            self.assertEqual(rows[0][0], name)
            target = rows[0][1]
            self.assertEqual(target['gtin13'], gtin)
            url = target['url']
            statuses[url] = 'unverified' if url == teknosa else 'unavailable'
            return {}, {}

        with patch('tws_refresh.reread', side_effect=collect):
            updated, _ = refresh(snapshot, catalog)
        self.assertFalse(updated['models'][0]['offers'])
        self.assertEqual([t['url'] for t in updated['sourceTargets'][name]], [vatan, teknosa])

        def restored(host, rows, statuses):
            url = rows[0][1]['url']
            statuses[url] = 'verified'
            offer = next(o for o in prior if o['url'] == url)
            return {name: [{**offer, 'observed_at': 'new', 'gtin13': gtin, '_legal_name': offer['merchant']}]}, {}

        with patch('tws_refresh.reread', side_effect=restored):
            updated, report = refresh(updated, catalog)
        self.assertEqual(len(updated['models'][0]['offers']), 2)
        self.assertEqual(report['refreshed'], 1)

    def test_transport_failure_preserves_original_timestamps(self):
        name = 'Model'
        offer = {'url': 'https://www.n11.com/urun/uyumlu-kulaklik-123456', 'merchant': 'Independent',
                 'observed_at': 'original', 'price': 1000.0}
        catalog = {'models': [{'name': name}]}
        snapshot = {'checked_at': 'old', 'models': [{'name': name, 'gtin': '0194644298876', 'offers': [offer]}]}
        with patch('tws_refresh.reread', return_value=({}, {'http_429': 1})):
            updated, report = refresh(snapshot, catalog)
        self.assertEqual(updated['checked_at'], 'old')
        self.assertEqual(updated['models'][0]['offers'], [offer])
        self.assertEqual(report['retained'], 1)
