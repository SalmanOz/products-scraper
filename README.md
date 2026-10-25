# Teknoskor - Akıllı Telefon Karşılaştırma Platformu

Bu depo, Türkiye'nin en kapsamlı cep telefonu özellik ve fiyat karşılaştırma sitesi olan [Teknoskor](https://teknoskor.com) platformuna ait yardımcı veri senkronizasyon araçlarını içermektedir.

[Teknoskor](https://teknoskor.com), kullanıcılara en güncel cep telefonu teknik özelliklerini sunmakta ve farklı mağazalardaki en ucuz fiyatları karşılaştırmalı olarak listelemektedir. 

## İncelenmiş telefon grubu ekleme

### Günlük fiyatların yeniden doğrulanması

`update_prices.py`, kaydedilmiş mağaza bağlantılarını ve SKU kimliği hâlâ aynı
olan incelenmiş URL ipuçlarını her çalışmada yeniden okur. URL ipuçları
`offer_batches/budget-eight-2026-10-25.json` içindedir; fiyat, stok veya eski
tarayıcı gözlem tarihi günlük işe taşınmaz. `browser-observations-*.json` yalnız
önceki açık onaylı, süresi sınırlı aktarım içindir; günlük kod bu dosyayı açmaz.

Keşif/arama kartları yalnızca mağaza URL'si sağlar. Güncelleme için mağazanın
kendi tam ürün/teklif kaydı, fiziksel RAM/depolama, model/radyo, TRY peşin fiyat,
stok ve gerçek gözlem zamanı yeniden doğrulanır. Sepete/üyeliğe/kupona bağlı
fiyatın koşulsuz fiyat olarak aktarılması engellenir; standart fiyat bu nedenle
doğrulanamıyorsa bir tahmin üretilmez. Ön sipariş ve bilinmeyen stok, satın
alınabilir mağaza kapsamına sayılmaz. Aynı alan adının farklı satıcı adlarıyla
iki mağaza gibi sayılması engellenir.

403/429/challenge cevabında o mağaza için aynı çalışmada devre kesici açılır;
PDP okuyucu engel çözmez veya eski gözlemi tazelemez. Geçici okuma hatası mevcut
teklifleri silmez, fakat canlı sitenin 72 saatlik tazelik sınırını da uzatmaz.
Dolayısıyla engellenen mağazalar için izinli ürün/fiyat feed'i veya resmî API
olmadan sürekli iki kaynak kapsamı garanti edilemez.

Her günlük iş, başarısız olsa da `daily-price-report.json` artifact'ını 30 gün
saklar. Raporda ürün başına gerçek gözlem zamanı, satın alınabilir mağazalar,
erişim/şema/kimlik hataları ve iki mağazası eksilen modeller bulunur. Mevcut
%50 genel kapsam eşiği korunur; ayrıca tamamlanan yedi bütçe modelinde iki canlı
mağaza kaybolursa iş başarısız olarak bildirilir. Bu kontrol gerçek fiyat
güncellemelerini geri almaz ve puan yayın eşiğini düşürmez. Spark Slim 5G,
belirsiz radyo kimliği çözülmediği için tamamlanmış çift kaynak grubuna katılmaz.

Gerçek günlük kodu üretime yazmadan kontrol etmek için **Reviewed Budget Offer
Recovery → mode: daily-audit** kullanılır. Bu mod yalnız bilinen PDP'leri okur;
ürün/teklif/şema/R2 yazmaz, IndexNow veya challenge çözümü çalıştırmaz. Veri
kapsamı yetersizse testler geçse bile denetim kırmızı olur; nedeni artifact'tadır.

### Yeni model grubu

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

Teknik veri yedeği olarak vivo'nun **Türkiye parametre sayfaları**, Apple ve OPPO
Türkiye teknik tabloları desteklenir. Xiaomi/Redmi/POCO için komşu etiket/değer
blokları ve tam model Product yapılandırılmış verisi; Samsung için tam model
Product yapılandırılmış verisi okunabilir. Samsung'un ilk HTML'inde boş gelen
dinamik tablo desteklenmiş sayılmaz; `manufacturer_structured_specs_unavailable`
ile açıkça raporlanır. Xiaomi'nin 403 yanıtı aşılmaz, kaynak o işte tekrar denenmez.
Yalnızca üreticinin ürün sayfasında gerçekten verdiği, aynı modele ait teknik
özellik bağlantısı izlenir; global varyanta veya tahmini bir URL'ye geçilmez.
Canonical bulunmayan sayfalarda tam model başlığı ve modele özel parametre
bağlantısı birlikte kimlik kanıtıdır. Çelişkili canonical bu yolu da kapatır.

Üreticiden alınan fiziksel bilgiler `manufacturer` kaynak türüyle kaydedilir;
Kimovil kaynağı gibi etiketlenmez. Eksik üst düzey alanlar tamamlanır, var olan
kaynak grubunun üzerine sessizce yazılmaz. RAM/depolama/pil/ekran çelişkisi
otomatik yayını durdurur. Genişletilmiş RAM fiziksel RAM'e eklenmez; birden fazla
SKU varsa keyfî olarak ilk kapasite seçilmez. Ürün renk görselleri de resmi
parametre sayfasından bulunabilir; özellikleri hâlâ eksik adaylar için R2'ye
gereksiz yedek görsel yüklenmez.

Görsel bulunamaması artık toplanmış teknik kayıtların kaybolmasına neden olmaz.
Raporun `collected_sources`, `missing_fields`, `official_image_candidates` ve
`conflicting_fields` alanları hangi bilgilerin tamamlandığını gösterir. Kısmi
veriler yalnızca job artifact'ında tutulur; bunlar yayımlanmış ürün değildir.
Apple ortak Pro/Pro Max tablolarında sadece tam modele ait sütun veya açıkça
ortak işaretlenmiş hücre kullanılır. İsimsiz iki sütun sırayla eşlenmez. Apple'ın
belirtmediği RAM ve mAh tahmin edilmez; OPPO'nun Kamera ve Video bölümlerindeki
aynı isimli satırlar ayrı korunur. OPPO Türkiye kataloğu günlük keşfe dahildir.

Puan ve çıkış tarihi teknik özelliklerden tahmin edilmez; mevcut yayın eşiği
korunur. Açık `releaseDate` veya kimliği doğrulanmış Kimovil teknik tablosundaki
`Release date` alanı kullanılabilir; `datePublished`, `dateModified`, sayfa altlığı
ve haber tarihi çıkış tarihi değildir. `February 2026, 7 months ago` ifadesi
`February 2026` olur; kesin gün uydurulmaz. Kimovil kategori girdileri yalnızca
sonlu, 0'dan büyük ve en fazla 10 olan değerlerdir; fiyat bağımlı toplam Ki puanı, kullanıcı oyu, AnTuTu ham
sonucu veya DXOMARK puanı bu girdilerin yerine dönüştürülmez.

### 19 Ekim 2026 eksik veri incelemesi (yayın girdisi değildir)

Normal HTTPS ile OPPO Reno15 5G Türkiye kaynağından 10 fiziksel kayıt (12/256 GB,
6500 mAh, 6,59 inç ve altı teknik grup), iPhone 18 Pro'dan 7 kayıt doğrulandı.
Bu kontrol R2/ürün/teklif veritabanına yazmadı. Puanlar ve eksik zorunlu alanlar
tamamlanmadığından bu kayıtlar yayına uygun bir yeni ürün sayılmaz.

Kimovil'in herkese açık sayfaları ayrıca araştırma tarayıcısında incelendi:

| Model | Kaynaktaki açık çıkış ayı (Türkiye satış tarihi değil) | Otomatik puan aktarımını durduran durum |
| --- | --- | --- |
| [vivo V70](https://www.kimovil.com/en/where-to-buy-vivo-v70) | Şubat 2026 | Varsayılan Global 8/256 GB, resmi TR 12/256 GB ile aynı SKU değil. |
| [vivo Y05](https://www.kimovil.com/en/where-to-buy-vivo-y05) | Şubat 2026 | International 4/64 GB, resmi TR 4/128 GB ile aynı SKU değil. |
| [vivo V70 FE](https://www.kimovil.com/en/where-to-buy-vivo-v70-fe) | Mart 2026 | Kaynak Global 8/256 GB; TR sayfasındaki çoklu bellek seçeneklerinden keyfî seçim yapılamaz. |

Bu araştırma notları otomasyon tarafından içeri alınmaz ve gözlem tarihleri
yenilenmiş gibi gösterilmez. Araştırma tarayıcısının okuyabildiği bir sayfanın
Actions'ın normal HTTP istemcisinden de erişilebilir olduğu varsayılmaz.
Normal istemcinin Kimovil 403 yanıtı sürerse kategori puanları eksik kalır ve
ürün, kullanıcı kararı gereği, doğrulanmış puan bulunana kadar bekletilir.

Test: `python -m unittest -q test_daily_discovery test_manufacturer_specs test_retailer_offer_refresh test_price_refresh_orchestration test_spec_sections test_source_ingestion`.

## Teknik özellik alanlarını onarma

Sınıflandırma okumasında A–E ölçeğinin tamamı değil, kaynağın `<b>`/`<strong>`
ile işaretlediği tek harf korunur. Enerji verimliliğinde `.eftag > .value`
okunur; A–G aralık etiketleri sonuca katılmaz. Seçim yoksa veya çelişkiliyse
sınıf tahmin edilmez; varsa açık düşme sayısı ayrı korunur. Bu parser düzeltmesi
eski veritabanı kayıtlarını kendiliğinden onarmaz. Web gösterimi, seçimi kaybolmuş
eski `Class A B C D E` / `AGA` kayıtlarını doğrulanmış sınıf gibi göstermez.

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
