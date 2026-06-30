from tr_price_scraper import TRPriceScraper

def test_resolve():
    scraper = TRPriceScraper()
    # A known Cimri offer URL
    offer_url = "https://www.cimri.com/offer/1980150603"
    logging.info(f"Original URL: {offer_url}")
    
    # Let's see if FlareSolverr can just load it and we get the final URL
    html = scraper.get_via_flaresolverr(offer_url)
    
    # We don't care about HTML, we want the final URL. But FlareSolverr might not return the final URL directly in our basic setup.
    # Actually, FlareSolverr returns the final URL if we ask for it, but our current implementation just returns HTML.
    logging.info("HTML length:", len(html) if html else 0)
    
    # Let's check if the HTML contains a JS redirect or meta refresh
    if html:
        if "window.location" in html or "meta http-equiv=\"refresh\"" in html.lower():
            logging.info("Found redirect in HTML!")
            # Extract simple meta refresh
            import re
            match = re.search(r'url=([^"]+)', html, re.IGNORECASE)
            if match:
                logging.info("Meta Refresh URL:", match.group(1))

if __name__ == "__main__":
    test_resolve()
