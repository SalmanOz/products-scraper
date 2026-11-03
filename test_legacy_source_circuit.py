import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from tr_price_scraper import TRPriceScraper


class LegacySourceCircuitTests(unittest.TestCase):
    def test_direct_forbidden_response_stops_repeated_requests_for_the_run(self):
        scraper = TRPriceScraper()
        get = Mock(return_value=SimpleNamespace(status_code=403, text='denied', url='https://www.epey.com/ara/'))
        with patch.dict('sys.modules', {'curl_cffi': SimpleNamespace(requests=SimpleNamespace(get=get))}), patch('tr_price_scraper.requests.post') as browser:
            self.assertIsNone(scraper.get_via_flaresolverr('https://www.epey.com/ara/?ara=phone'))
            self.assertIsNone(scraper.get_via_flaresolverr('https://www.epey.com/another-phone.html'))
        self.assertEqual(get.call_count, 1)
        browser.assert_not_called()

    def test_solver_ok_with_http_403_is_not_successful_source_evidence(self):
        scraper = TRPriceScraper()
        response = Mock()
        response.json.return_value = {'status': 'ok', 'solution': {'status': 403, 'response': 'blocked'}}
        with patch.object(scraper, '_try_curl_cffi', return_value=None), patch('tr_price_scraper.requests.post', return_value=response) as browser:
            self.assertIsNone(scraper.get_via_flaresolverr('https://www.epey.com/phone'))
            self.assertIsNone(scraper.get_via_flaresolverr('https://www.epey.com/phone2'))
        self.assertEqual(browser.call_count, 1)
        self.assertIn('www.epey.com', scraper.dead_domains)

    def test_real_browser_200_remains_usable(self):
        scraper = TRPriceScraper()
        solution = {'status': 200, 'response': '<html>product listing</html>', 'url': 'https://shop.example/phone'}
        response = Mock()
        response.json.return_value = {'status': 'ok', 'solution': solution}
        with patch.object(scraper, '_try_curl_cffi', return_value=None), patch('tr_price_scraper.requests.post', return_value=response):
            self.assertEqual(scraper.get_via_flaresolverr(solution['url'], return_solution=True), solution)


if __name__ == '__main__':
    unittest.main()
