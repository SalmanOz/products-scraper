# Teknoskor - Akıllı Telefon Özellik & Fiyat Scraper Backend

Bu depo, Türkiye'nin lider akıllı telefon karşılaştırma platformu olan [Teknoskor](https://teknoskor.com) için geliştirilmiş bağımsız (standalone) veri çekme ve fiyat güncelleme arka plan servisidir.

[Teknoskor.com](https://teknoskor.com) üzerinde yayınlanan cep telefonlarının teknik özelliklerini Kimovil ve GSM Arena gibi global kaynaklardan senkronize eder; Hepsiburada, Trendyol, Amazon TR, Vatan Bilgisayar gibi popüler Türkiye satıcılarındaki fiyatları güncelleyerek veritabanını her gün taze tutar.

## 🚀 Özellikler

* **Teknik Detay Senkronizasyonu:** Yeni akıllı telefonları global veritabanlarından çekerek Türkçe özellik eşleştirmeleriyle sisteme dahil eder.
* **Otomatik Fiyat Güncelleme:** Türkiye'deki 10'dan fazla pazaryeri ve satıcının (Trendyol, Hepsiburada, Amazon vb.) anlık fiyatlarını tarar.
* **Fiyat Geçmişi Loglama:** Fiyat değişimlerini tarihsel olarak veritabanına işleyerek web arayüzünde fiyat değişim grafiği (Price History Chart) oluşmasını sağlar.
* **Cloudflare & Bot Bypass:** Taramalar esnasında bot korumalarını aşmak için entegre FlareSolverr (Docker tabanlı) desteği sunar.
* **Serverless Çalışma:** GitHub Actions üzerinde her gece otomatik çalışacak şekilde optimize edilmiştir, böylece web sunucusuna (Hostinger vb.) ekstra yük bindirmez.

## 📋 Gereksinimler

* Python 3.9+
* MySQL Veritabanı
* Docker (FlareSolverr için - lokal çalıştırmada isteğe bağlı, GitHub Actions üzerinde otomatik kurulur)
* Cloudflare R2 (Görselleri WebP formatında CDN üzerinde depolamak için - İsteğe bağlı)

## 🛠️ Kurulum ve Lokal Çalıştırma

1. Bu projeyi klonlayın:
   ```bash
   git clone https://github.com/KULLANICI_ADINIZ/products-scraper.git
   cd products-scraper
   ```

2. Bağımlılıkları yükleyin:
   ```bash
   pip install -r requirements.txt
   ```

3. `.env` dosyası oluşturun ve veritabanı ile R2 bilgilerinizi girin:
   ```env
   DB_HOST=your_db_host
   DB_USER=your_db_user
   DB_PASSWORD=your_db_password
   DB_NAME=your_db_name
   DB_PORT=3306
   
   # İsteğe Bağlı Cloudflare R2 Ayarları
   R2_ACCOUNT_ID=your_r2_account_id
   R2_ACCESS_KEY_ID=your_r2_access_key_id
   R2_SECRET_ACCESS_KEY=your_r2_secret_key
   R2_BUCKET_NAME=your_bucket_name
   R2_PUBLIC_DOMAIN=https://cdn.teknoskor.com
   ```

4. FlareSolverr servisini Docker üzerinde başlatın (Cloudflare korumalarını aşmak için gereklidir):
   ```bash
   docker run -d --name=flaresolverr -p 8191:8191 -e LOG_LEVEL=info ghcr.io/flaresolverr/flaresolverr:latest
   ```

5. Fiyat güncelleyiciyi çalıştırın:
   ```bash
   python update_prices.py
   ```

## ⚙️ GitHub Actions ile Otomasyon (Cronjob)

Bu proje, paylaşımlı hosting (Hostinger vb.) kullanan web sitelerinin kaynak sınırlarına takılmadan çalışabilmesi için **GitHub Actions** entegrasyonuyla tasarlanmıştır.

Her gece TSİ 06:00'da otomatik tetiklenerek:
1. Geçici bir Ubuntu sunucusu kurar.
2. Docker üzerinde FlareSolverr'ı ayağa kaldırır.
3. Python bağımlılıklarını yükler.
4. `main.py` ve `update_prices.py` scriptlerini çalıştırarak veritabanınızı otomatik günceller.

Bu otomasyonu aktifleştirmek için deponuzun **Settings > Secrets > Actions** ayarlarına giderek `.env` içerisindeki tüm veritabanı değişkenlerini eklemeniz yeterlidir.

---

Katkıda bulunmak, hata bildirmek veya öneride bulunmak için bir Issue açabilir ya da Pull Request gönderebilirsiniz.

Platformu incelemek için: [Teknoskor.com](https://teknoskor.com)
