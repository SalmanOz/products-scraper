"""Offline tests for blog_quality_check's deterministic layer. No network/DB.

Run: python3 test_blog_quality.py
(llm_judge is not exercised here — deterministic_checks must gate correctly
on its own when the API is unavailable.)
"""
from blog_quality_check import check_facts, deterministic_checks

SOURCE = [
    {"name": "Xiaomi 15T", "base_price": 38499, "teknoskor_score": 82,
     "attributes": {"antutu_score": 1932053, "battery_mah": 5500},
     "images": ["https://cdn.teknoskor.com/products/xiaomi-15t/xiaomi-15t-2.webp"]},
    {"name": "Samsung Galaxy S25", "base_price": 56799, "teknoskor_score": 89,
     "attributes": {"antutu_score": 2150000, "battery_mah": 4900}, "images": []},
]

GOOD_ARTICLE = """
<h2 id="giris">Neden bu ikili?</h2>
Fiyat etiketi 38.499 TL olan Xiaomi 15T, kağıt üzerinde 56.799 TL'lik rakibinden çok daha ucuz.
Peki 1.932.053 AnTuTu puanı günlük hayatta ne demek? Metroda navigasyon açıkken arka planda müzik takılmıyor demek.
Kısa cevap: bütçe öncelikliyse 15T. Ama ekran kalitesi senin için pazarlık konusu değilse durum değişir.
<h2 id="batarya">Batarya gerçeği</h2>
5500 mAh kapasite, skorlara göre iki güne yakın kullanım sağlıyor. Rakipte bu değer 4900 mAh.
<a href="/product/xiaomi-15t">Xiaomi 15T</a> ve <a href="/karsilastir/samsung-galaxy-s25-vs-xiaomi-15t">detaylı karşılaştırma</a> sayfalarına bakabilirsin.
<h2 id="hukum">Hüküm</h2>
Veriler net: puan farkı 7, fiyat farkı 18 bin TL'nin üzerinde. Bu farkı ekran ve kamera kapatmıyor.
""" * 12  # repeat to clear the 1000-word floor


def test_fact_check_catches_hallucinated_numbers():
    bad = GOOD_ARTICLE + "\nAyrıca bu telefon 99.999 TL'lik özel sürümüyle 3.500.000 AnTuTu puanı alıyor."
    problems = check_facts(bad, SOURCE)
    assert any("99.999" in p or "99999" in p for p in problems), f"price hallucination missed: {problems}"
    assert any("3.500.000" in p or "3500000" in p for p in problems), f"antutu hallucination missed: {problems}"


def test_fact_check_accepts_source_numbers():
    problems = check_facts(GOOD_ARTICLE, SOURCE)
    assert problems == [], f"false positives on legit numbers: {problems}"


def test_gate_fails_on_cliches_and_fabricated_testing():
    bad = GOOD_ARTICLE + "\nTestlerimizde adeta ezber bozan bir cihaz gördük."
    det = deterministic_checks("Farklı Bir Başlık", "Geçerli uzunlukta bir özet cümlesi buraya geliyor.", bad, SOURCE)
    joined = " ".join(det["failures"])
    assert "klişe" in joined.lower() or "adeta" in joined
    assert "test" in joined.lower(), f"fabricated-experience miss: {det['failures']}"


def test_gate_fails_on_banned_title_and_similar_title():
    det = deterministic_checks("2026'nın En İyi Telefonları", "Geçerli uzunlukta bir özet cümlesi buraya geliyor.", GOOD_ARTICLE, SOURCE,
                               recent_titles=["2026'nın En İyi Telefonları: Büyük Rehber"])
    joined = " ".join(det["failures"])
    assert "başlık kalıbı" in joined.lower()
    assert "benziyor" in joined


def test_gate_flags_foreign_images_and_short_content():
    short = '<img src="https://evil.example/x.jpg" /> Kısa yazı.'
    det = deterministic_checks("Başlık", "Geçerli uzunlukta bir özet cümlesi buraya geliyor.", short, SOURCE)
    joined = " ".join(det["failures"])
    assert "görsel" in joined.lower()
    assert "kısa" in joined.lower()


def test_clean_article_passes_deterministic_layer():
    det = deterministic_checks(
        "Xiaomi 15T mi Galaxy S25 mi: 18 Bin TL'lik Fark Neyi Alıyor?",
        "İki modelin skor, batarya ve fiyat verilerini yan yana koyup net bir hüküm veriyoruz.",
        GOOD_ARTICLE, SOURCE, recent_titles=["Bambaşka Bir Konu Başlığı"])
    assert det["failures"] == [], f"clean article should pass: {det['failures']}"


if __name__ == "__main__":
    test_fact_check_catches_hallucinated_numbers()
    print("✅ fact check catches hallucinated price + AnTuTu")
    test_fact_check_accepts_source_numbers()
    print("✅ fact check accepts legit source numbers")
    test_gate_fails_on_cliches_and_fabricated_testing()
    print("✅ gate fails on clichés + fabricated testing claims")
    test_gate_fails_on_banned_title_and_similar_title()
    print("✅ gate fails on banned/duplicate titles")
    test_gate_flags_foreign_images_and_short_content()
    print("✅ gate flags foreign images + thin content")
    test_clean_article_passes_deterministic_layer()
    print("✅ clean article passes")
