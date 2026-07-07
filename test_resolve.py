from tr_price_scraper import TRPriceScraper

def test_resolve():
    scraper = TRPriceScraper()
    # Test with a known merchant URL
    offer_url = "https://www.hepsiburada.com/test-product"
    logging.info(f"Original URL: {offer_url}")
    
    # Test FlareSolverr loading
    html = scraper.get_via_flaresolverr(offer_url)
    
    logging.info("HTML length:", len(html) if html else 0)

if __name__ == "__main__":
    test_resolve()
