# Teknoskor - Akıllı Telefon Karşılaştırma Platformu

Bu depo, Türkiye'nin en kapsamlı cep telefonu özellik ve fiyat karşılaştırma sitesi olan [Teknoskor](https://teknoskor.com) platformuna ait yardımcı veri senkronizasyon araçlarını içermektedir.

[Teknoskor](https://teknoskor.com), kullanıcılara en güncel cep telefonu teknik özelliklerini sunmakta ve farklı mağazalardaki en ucuz fiyatları karşılaştırmalı olarak listelemektedir. 

## İncelenmiş telefon grubu ekleme

Model listeleri `catalog_batches/<batch_id>.json` dosyalarında tutulur. Yeni bir
grup eklemek için mevcut JSON biçimini kullanın; dosya adı ile `batch_id` aynı
olmalı, grupta 1–20 farklı model bulunmalıdır. Her modelin adı, URL kısa adı,
markası, resmî üretici ürün sayfası ve `new_release` / `popular_missing` seçimi
incelemeden geçmelidir. Henüz satışa çıkmamış veya kimliği doğrulanmamış modelleri
satıştaki ürün grubuna katmayın. Dosyalara parola veya erişim anahtarı koymayın.

İlk geçişte, ürün listesini istek gövdesinden doğrulayan `products` API sürümünü
canlıya alın. Sonraki model grupları yalnızca bu depoda değişiklik gerektirir.
Yalnızca `batch_id` gönderen eski istemci artık desteklenmez.

GitHub Actions → Daily Price & Data Sync → Run workflow:

- `run_technical_data`: true
- `technical_pending_only`: true
- `catalog_expansion_batch`: incelenmiş JSON dosyasının uzantısız adı
- Diğer seçenekler: varsayılan değerler

Akış önce kayıtları oluşturur, sonra teknik veri ve görselleri doğrular, ardından
Türkiye mağaza fiyatlarını arar. Yeniden çalıştırıldığında aynı kimlikteki kayıtlar
çoğaltılmaz. Eksik kaynak verisi mevcut doğrulama kurallarını aşamaz. Çalışma
sonunda oluşturulan/doğrulanan modelleri ve canlı ürün sayfalarını kontrol edin;
fiyatı bulunamayan model için fiyat uydurmayın.

Yeni dosyaları göndermeden önce `python test_catalog_expansion.py` çalıştırın.
Bu eski akışın günlük zamanlaması yalnızca fiyatları günceller. Yeni model keşfi
aşağıdaki bağımsız kalite kapısından geçer; elle incelenen grup akışını kullanmaz.

## Günlük keşif ve güvenli otomatik yayın

`Daily Phone Discovery & Quality Gate` her gün yaklaşık **07:00 Türkiye saatiyle**
çalışır. GitHub zamanlamaları gecikebilir. Fiyat işleriyle aynı yazma kilidini
kullanır. İlk sürüm Apple, Samsung, Xiaomi/Redmi/POCO ve vivo'nun Türkiye ürün
listelerini tarar; diğer markalar henüz otomatik kapsamda değildir. Kaynak 403,
429, doğrulama ekranı veya değişen HTML döndürürse güvenlik engeli aşılmaz; durum
raporlanır. Bu iş FlareSolverr kullanmaz.

Yeni model adayları mevcut adlar/URL'ler ile karşılaştırılır. RAM/depolama
varyantları yeni model sayılmaz; açıkça farklı 4G/5G modelleri ayrı kalabilir.
Her çalışmada en fazla sekiz aday incelenir, önceki deneme tarihine göre sırayla
yeniden değerlendirilir. Bir günde en fazla iki model yayımlanabilir; bu üst sınır
Türkiye takvim gününe göre sunucuda da uygulanır. **Asgari ekleme kotası yoktur.**

Otomatik yayın için tekil üretici Türkiye ürün kimliği, kaynak/hash kapsamı tam
teknik veriler (kamera, işlemci, bağlantı, yazılım, ekran, pil dahil), geçmiş çıkış
tarihi, gerçek CDN ürün görseli ve son altı saatte doğrudan okunmuş stoklu TRY
teklifi gerekir. Fiyatın model, fiziksel RAM/depolama ve ağ nesli kanıtları
eşleşmelidir. iPhone için mevcut model+depolama RAM istisnası korunur. Arama
kartı/karşılaştırma sitesi fiyatı tek başına yetmez; mağazanın kendi Product/Offer
kaydı tekrar okunur. İthalatçı/yenilenmiş ibaresi veya çelişkili fiyatlar otomatik
yayını durdurur. Bu kontrol garanti bölgesinin tam hukuki doğrulaması değildir.

Sunucu kalite kapısını bağımsız uygular. Ürün, kaynaklar, teklifler ve ilk fiyat
geçmişi **tek transaction** içinde kaydedilir; yarım ürün sayfası yayımlanmaz.
Mevcut ürünler değiştirilmez. Kaynak eksikliği sahte özellik veya fiyatla
doldurulmaz; tüm kombinasyonlardan karşılaştırma sayfası üretilmez.

Elle denemek için Actions → bu workflow → `publish=false` seçin. Bu mod ürün/teklif
veritabanına yazmaz; gerçek görsel kontrolü için aday görselleri mevcut R2 alanına
yüklenebilir. `publish=true` kaliteyi geçen adayları yayımlar. Zamanlanmış günlük
işte yayın açıktır. Aynı ingestion ve R2 secret'ları kullanılır; yeni erişim anahtarı
veya ücretli servis gerekmez. Canlı API `catalog-discovery-v1` hazır değilse iş
kapalı şekilde başarısız olur, eski toplu ekleme yoluna dönmez.

Her iş `daily-discovery-report.json` artifact'ında 30 gün boyunca adayları,
bekleme nedenlerini ve oluşturulan modelleri saklar; iş özetinde de kararlar
görünür. `.discovery-state` önbelleğinde yalnızca herkese açık model kimlikleri ve
son deneme tarihleri vardır, secret yoktur. Cache kaybolursa güvenlik kapısı ve
sunucu günlük limiti yine korunur.

Test: `python -m unittest -q test_daily_discovery test_retailer_offer_refresh test_price_refresh_orchestration`.

## Teknik özellik alanlarını onarma

Scraper işlemci/RAM/depolama tiplerini ve arka/ön kamera alanlarını ayrı tutar.
Belirsiz saat hızlarını (`XX GHz`) ve çakışan, bağlamı bilinmeyen alanları aktarmaz.
Eski düzleştirilmiş kayıtlar için önce `python repair_spec_mapping.py --batch
<batch_id>` ile değişiklik raporunu inceleyin; bu komut yazma yapmaz. `--apply`
yalnızca bu gruptaki mevcut modellerin donanım ve kamera gruplarını değiştirir.
Fiyat, görsel, diğer teknik gruplar ve ürün kimliği korunur. Kaynak/model eşleşmesi
ve gerekli alanlar doğrulanmazsa hiçbir onarım gönderilmez.

Actions üzerinden uygulamak için `run_technical_data=true`,
`repair_spec_batch=<batch_id>`, diğer seçenekler varsayılan olmalıdır.
İşlem öncesi/sonrası değerler 30 gün saklanan `spec-mapping-repair` çıktısındadır.
Bu işlem yeni ürün eklemez; günlük fiyat taramasının davranışını değiştirmez.

## 🔗 Hızlı Bağlantılar

* **Ana Sayfa:** [Teknoskor.com](https://teknoskor.com)
* **Telefon Karşılaştırma:** [Teknoskor Telefon Karşılaştırma](https://teknoskor.com)
* **Akıllı Telefon Özellikleri:** [Teknoskor Cep Telefonları](https://teknoskor.com)

---
*Bu depo sadece Teknoskor altyapısında veri senkronizasyonu sağlamak amacıyla tasarlanmış yardımcı betikleri barındırır. Genel kullanıma veya kuruluma yönelik destek sunulmamaktadır.*
