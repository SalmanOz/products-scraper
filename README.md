# Teknoskor Python Scraper (Kimovil)

Bu klasör, Teknoskor platformu için teknik verileri toplayan bağımsız (standalone) Python scriptlerini içerir. Lokal bilgisayarınızda çalıştırılmak üzere tasarlanmıştır.

## Gereksinimler

- Python 3.8+
- MySQL Veritabanı Erişimi

## Kurulum

1. Bu klasöre gidin:
   ```bash
   cd scraper_python
   ```

2. Bağımlılıkları yükleyin:
   ```bash
   pip install -r requirements.txt
   ```

3. Playwright tarayıcısını kurun:
   ```bash
   playwright install chromium
   ```

4. `.env` dosyasını düzenleyin:
   - Hostinger'daki veritabanı bilgilerini girin.

## Çalıştırma

Scraper'ı başlatmak için:
```bash
python main.py
```

## Neden Bağımsız?

Hosting firmaları (Hostinger vb.) genellikle Docker ve tarayıcı otomasyonu (Playwright) çalıştırmaya izin vermez. Bu yüzden verileri kendi bilgisayarınızda çekip, script üzerinden doğrudan sunucudaki veritabanına aktarıyoruz.
