import logging
import requests
import re
from bs4 import BeautifulSoup

def test_listing():
    url = "http://localhost:8191/v1"
    payload = {
        "cmd": "request.get",
        "url": "https://www.kimovil.com/en/compare-smartphones",
        "maxTimeout": 60000
    }
    response = requests.post(url, json=payload).json()
    if response['status'] == 'ok':
        html = response['solution']['response']
        soup = BeautifulSoup(html, 'html.parser')
        urls = []
        for a in soup.find_all('a', href=re.compile(r'where-to-buy')):
            href = a.get('href')
            logging.info(f"Found href: {href}")
            urls.append(href)
        logging.info(f"Total found: {len(urls)}")
    else:
        logging.info("FlareSolverr failed")

if __name__ == "__main__":
    test_listing()
