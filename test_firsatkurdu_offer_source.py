import unittest

from firsatkurdu_offer_source import BASE, COMPANY, TERMS, selected_offer, seller_identity


URL = BASE + '/urun/tecno-buds-4-air-beyaz-11417'
TITLE = 'Tecno Buds 4 Air Beyaz'


def listing():
    return f'''<link rel="canonical" href="{URL}">
    <script>var pageParams = {{product: {{id: "4597", sku: "7707", name: "{TITLE}"}}}};</script>
    <div itemscope itemtype="https://schema.org/Product">
        <span itemprop="name">{TITLE}</span><span itemprop="sku">7707</span>
        <meta itemprop="gtin13" content="4894947052446">
        <div itemprop="brand"><meta itemprop="name" content="Tecno"></div>
        <span itemprop="category" content="Kablosuz Kulaklıklar"></span>
        <meta itemprop="itemCondition" content="http://schema.org/NewCondition">
        <div itemprop="offers" itemscope itemtype="https://schema.org/Offer">
            <meta itemprop="price" content="1399.00"><meta itemprop="priceCurrency" content="TRY">
            <link itemprop="availability" href="https://schema.org/InStock"><link itemprop="url" href="{URL}">
        </div>
    </div>
    <main id="product-detail-container"><h1>{TITLE}</h1>
        <div class="product-price"><div class="product-price-old">1.399,00 TL</div>
            <div class="product-price-title">KDV Dahil</div></div>
        <a data-context="detail" data-product-id="4597" data-selector="add-to-cart">Sepete Ekle</a>
    </main>'''


class FirsatKurduOfferSourceTest(unittest.TestCase):
    def test_selected_listing_agrees_with_cart_price_and_terms(self):
        terms = f'<link rel="canonical" href="{TERMS}"><p>Ünvanı : MATRİS TEKNOLOJİ ÜRÜNLERİ VE ELEKTRONİK LTD. ŞTİ.</p>'
        self.assertEqual(seller_identity(terms), COMPANY)
        self.assertIsNone(seller_identity(terms.replace(TERMS, BASE)))
        offer, reason = selected_offer(listing(), URL, COMPANY, kind='tws')
        self.assertEqual(reason, 'verified')
        self.assertEqual((offer['gtin13'], offer['price'], offer['sku'], offer['_legal_name']),
                         ('4894947052446', 1399, '7707', COMPANY))
        self.assertTrue(offer['observed_at'].endswith('+00:00'))

    def test_rejects_disagreement_between_selected_product_and_purchase_controls(self):
        for before, after in [('InStock', 'OutOfStock'), ('1.399,00 TL', '999,00 TL'),
                              ('data-product-id="4597"', 'data-product-id="4598"'),
                              ('4894947052446', '4894947052447'), ('KDV Dahil', 'Sepette KDV Dahil'),
                              ('Sepete Ekle', 'Gelince Haber Ver')]:
            with self.subTest(change=after):
                self.assertIsNone(selected_offer(listing().replace(before, after), URL, COMPANY, kind='tws')[0])
