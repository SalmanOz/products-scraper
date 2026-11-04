# Teknoskor - Akıllı Telefon Karşılaştırma Platformu

Bu depo, Türkiye'nin en kapsamlı cep telefonu özellik ve fiyat karşılaştırma sitesi olan [Teknoskor](https://teknoskor.com) platformuna ait yardımcı veri senkronizasyon araçlarını içermektedir.

[Teknoskor](https://teknoskor.com), kullanıcılara en güncel cep telefonu teknik özelliklerini sunmakta ve farklı mağazalardaki en ucuz fiyatları karşılaştırmalı olarak listelemektedir. 

## İncelenmiş telefon grubu ekleme

### Günlük fiyatların yeniden doğrulanması

`update_prices.py`, kaydedilmiş mağaza bağlantılarını ve SKU kimliği hâlâ aynı
olan incelenmiş URL ipuçlarını her çalışmada yeniden okur. URL ipuçları
`offer_batches/budget-eight-2026-10-25.json` ve
`offer_batches/vatan-exact-2026-11-02.json` içindedir; fiyat, stok veya eski
tarayıcı gözlem tarihi günlük işe taşınmaz. `browser-observations-*.json` yalnız
önceki açık onaylı, süresi sınırlı aktarım içindir; günlük kod bu dosyayı açmaz.

Keşif/arama kartları yalnızca mağaza URL'si sağlar. Güncelleme için mağazanın
kendi tam ürün/teklif kaydı, fiziksel RAM/depolama, model/radyo, TRY peşin fiyat,
stok ve gerçek gözlem zamanı yeniden doğrulanır. Sepete/üyeliğe/kupona bağlı
fiyatın koşulsuz fiyat olarak aktarılması engellenir; standart fiyat bu nedenle
doğrulanamıyorsa bir tahmin üretilmez. Ön sipariş ve bilinmeyen stok, satın
alınabilir mağaza kapsamına sayılmaz. Aynı alan adının farklı satıcı adlarıyla
iki mağaza gibi sayılması engellenir.

Katalogda radyo nesli alanı eksikse yalnız incelenmiş profillerin aynı
ID/ad/fiziksel RAM/depolama kimliğiyle eşleşen kayıtları için gözden geçirilmiş
4G/5G profili kullanılır. Katalogda açıkça farklı radyo bilgisi varsa bu profil
onu değiştirmez ve URL tohumu devre dışı kalır. Mağaza sayfasındaki model ve
varyant doğrulaması yine zorunludur.

Doğrudan ürün sayfası okuması her çağrıda en fazla 12 farklı URL ile sınırlıdır.
Bir mağazanın çok sayıda renk bağlantısı diğer mağazaları bütçeden düşürmez:
önce her mağazadan bir bağlantı, sonra sırayla ek renkler denenir. Bütçe dışında
kalan güvenli URL'ler günlük raporda `target_budget_skipped` olarak görünür.

Öncelikli kaynaklar Hepsiburada, Trendyol, n11 ve Teknosa sırasıyla ilk URL
turunda yer alır. Hepsiburada ve Trendyol okuyucuları katalogdaki mevcut
bağlantılardan model/RAM/depolama profilini türetir; telefon başına yeni bir
elle hazırlanmış profil gerektirmez. Hepsiburada'da seçili Redux SKU/barkod,
görünen satıcı, teknik tablo, iki satın alma kutusu olayı ve peşin fiyat;
Trendyol'da seçili ürün/satıcı/ilan, iki seçili varyant listesi, barkod, teknik
alanlar, JSON-LD, görünen fiyat ve açık satın alma düğmesi birlikte doğrulanır.
Eski Hepsiburada `magaza=` ipucu, aynı SKU'nun sorgusuz ana sayfasından güncel
satıcıyı keşfetmek için kullanılır; dönen teklifin URL'si ve satıcısı yeni
gözlemden gelir. Eski satıcı adına fiyat aktarılmaz. Trendyol'un eski
`merchantId`/`boutiqueId`/`v=tek-ebat` keşif ipuçları da aynı ürün kimliğinin
sorgusuz PDP'sine çevrilir; güncel satıcı yeni seçili ilan kaydından kanıtlanır.
Doğrudan satıcıya özel parser çağrısında seçici hâlâ zorunludur.
Trendyol'da yalnız aynı
ürün kimliğini ve seçilmiş satıcıyı koruyan bir yönlendirme izlenebilir; bu
ikinci istek de mevcut kaynak kotasından düşer ve yeniden robots kontrolünden
geçer. İkinci yönlendirme veya değişen satıcı/ürün kimliği kabul edilmez.
Üretici adı pazar yeri satıcısı kabul edilmez. Satıcı başka bir URL'ye zaten
bağlıysa veya bütün kayıtlı satırlar kontrol edilemiyorsa teklif yayımlanmaz.
Teknosa'nın genel okuyucusu yalnız seçili TEKNOSA birinci taraf satıcısı,
mağaza 1001, SKU, EAN13, stok ve koşulsuz fiyat aynı olduğunda teklif hazırlar.

`Priority Retailer Catalog Read-only Audit`, katalogdan kaynak başına en çok
sekiz mevcut adresi otomatik örnekler. Bu denetim ürün, fiyat, puan veya
IndexNow yazmaz. HTTP 200 tek başına teklif değildir; stok veya varyant kanıtı
eksik sayfalar elenir. Trendyol'un 301 yanıtı satıcı seçicisini düşürdüğünden,
eski seçicinin satıcısı güncel seçili satıcı kabul edilmez. n11'de belge
okuma servisinin 500 hatası henüz çözülmüş sayılmaz. Her kaynakta robots,
istek aralığı, boyut/kota ve erişim hatası devre kesicisi uygulanır.
Tam katalog işinde bu okuyucuların kotası katalog büyüklüğüne ayarlanır
(mutlak üst sınır 200 belge); 110 ürünlük iş 60 belge sınırında kesilmez.
Salt okunur örneklem ve tek ürün varsayılan kotaları artırılmaz.

3 Kasım dördüncü üretim turu:
[Daily Price & Data Sync #222](https://github.com/SalmanOz/products-scraper/actions/runs/41610758334)
65 teklifi kabul etti (0 eski gözlem, 0 varyant reddi). Genel iş 109 ürünü
işledi; A37 mevcut eşli teklif akışına ayrıldığı için bu genel işte atlandı.
Canlı katalogda fiyatlı ürünler 40/110, iki mağazalı ürünler 19'dan 20'ye çıktı.
Turda yeniden doğrulanan kapsam 33/109 (%30,3) olduğundan %50 kalite eşiği
başarısız kaldı. Sonradan eklenen öncelikli kaynak okuyucuları bu üretim
turunun sabitlenmiş sürümünde yoktur; salt okunur denetimleri ayrı raporlanır.
A37 ayrıca mevcut eşli akışta
[bir kez denendi](https://github.com/SalmanOz/products-scraper/actions/runs/41617602530);
satıcı/hedef bütünlüğü ön kontrolü `merchant_name_cross_platform_collision`
ile durdu, iki tekliften hiçbiri yazılmadı. Genel akışın A37 koruması kaldırılmadı.

`Priority Retailer Automatic Candidate Refresh` zamanlamasız ve varsayılan
olarak yazımsızdır. `publish=true` ile elle başlatıldığında yalnız o çalışmanın
otomatik örnekleminden çıkan adayları en çok 24 teklifle sınırlar; her birinin
sayfasını tekrar okur, fiyat/SKU/satıcı/barkod değişimini reddeder, tam satıcı
hedefi ve varyant kontrollerinden geçenleri tek atomik HTTPS isteğinde yayımlar.
Yeni ürün, teknik özellik veya elle telefon profili oluşturmaz. A37 bu akışa
alınmaz. Kabul edilen teklifler sonrası mevcut puanlar yeniden hesaplanır.
Aynı satıcı/URL/fiyat son 30 dakikada zaten yayımlanmış ve mevcut ticari
filtrede görünüyorsa yeniden yazılmaz. Yeni kabul edilen her satırın da ticari
filtrede aynı URL, gerçek satıcı ve fiyatla göründüğü geri okunur.

Bu çalışma sırasında mevcut zamanlama ayrıca
[Daily Price & Data Sync #223](https://github.com/SalmanOz/products-scraper/actions/runs/41618023370)
işini başlattı. Yeni genel okuyucularla 69 teklif kabul edildi (0 eski, 0
varyant reddi); o turda canlı doğrulanan kapsam 35/109 olduğundan %50 eşiği
yine geçilmedi. Manuel olarak ikinci bir genel tur başlatılmadı. Güncel kamu
kataloğu 42/110 fiyatlı ürüne çıktı: A54 ve Galaxy S25 fiyatlandı, Camon 40'ın
en düşüğü 18.897 TL'den 16.999 TL'ye indi. Teklifler elle eklenmedi.

Bağımsız satıcı sayımında noktalama farkı düzeltilince çok satıcılı ürün sayısı
21 oldu (başlangıç 19). A54'ün Hepsiburada ve Trendyol'daki `MOBİLSTORE.33` /
`MOBİLSTORE33` teklifleri iki ayrı bağımsız satıcı sayılmaz; iki platformun
teklifleri görünmeye devam eder ve platform adı gerçek satıcıdan ayrı gösterilir.
[Son otomatik aday tamamlayıcısı](https://github.com/SalmanOz/products-scraper/actions/runs/41622434431)
32 ipucunu yeniden örnekledi; o örneklemde yeni geçerli aday çıkmadığı için
hiç fiyat yazmadı. n11 erişimi ve Teknosa'nın mevcut stok/satıcı kanıtı açıkları
çözülmüş sayılmaz.

403/429/challenge cevabında o mağaza için aynı çalışmada devre kesici açılır.
İncelenmiş Vatan ve MediaMarkt tam ürün bağlantılarında normal GET 403 dönerse
`robots.txt` izni doğrulanarak mevcut FlareSolverr ile salt GET denenir;
Vatan'da en fazla altı, MediaMarkt'ta en fazla 16 tarayıcı isteği yapılır.
Başka bağlantı ya da 429 bu yola girmez.
Geçici okuma hatası mevcut
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

### Ortaklık gerektirmeyen yeni fiyat kaynakları

25 Ekim 2026'da incelenen tüketici mağazaları: [Pozitif Teknoloji](https://www.pt.com.tr/)
ve [Troy / Artı Bilgisayar](https://www.troyestore.com/troy-hakkimizda). Bu iki
kaynak Apple içindir; engellenen Android bütçe modellerinin ikinci mağaza açığını
kapatmış sayılmaz. Örnekler: [PT iPhone 16 128 GB](https://www.pt.com.tr/iphone-16-128gb-deniz-mavisi-myed3tu-a),
[Troy iPhone 16 128 GB](https://www.troyestore.com/apple-iphone-16-128gb-deniz-mavisi-myed3tua_216591).

Günlük 03:00 UTC / 06:00 Türkiye işi `PublicRetailerSources` üzerinden canlı ana
sayfada gerçekten bağlantısı bulunan iPhone ailelerini ve tam depolama varyantını
bulur; aile sayfası fiyatını kullanmaz. Kaynak başına ana sayfa/robots/aile
önbelleği, 150 HTTP isteği sınırı, en az 500 ms istek aralığı (robots crawl-delay
daha uzunsa o süre), model başına en fazla altı renk bağlantısı vardır. Robots
okunamazsa veya izin vermiyorsa kaynak kapalı kalır. 403/429, sunucu/ulaşım hatası
ve challenge durumunda alternatif kimlik/proxy denenmez. Sepet, filtre, oturum,
ödeme, üyelik veya token URL'leri çağrılmaz; sorgusuz HTTPS bağlantıları kullanılır.

Mevcut tüm SKU/radyo/RAM/depolama, stok, tazelik, fiyat aykırılığı ve iki mağaza
şartları korunur. Yeni kaynaklar ayrıca kendi satıcı kimliğini, yeni ürün
durumunu, Apple Türkiye TU/A satış kodunu, ürün başlığını ve görünen standart
fiyatın tekil JSON-LD teklifiyle eşitliğini kanıtlamalıdır. PT için ana ürün
stok alanı ve tüm kartlara tek çekim seçeneği gerekir; aksesuar fiyatı/stoku
telefon fiyatı sayılmaz. Troy'un HTTP yazılmış şema URL'si yalnız aynı HTTPS
canonical ve aynı SKU için belge içinde düzeltilir; hiçbir HTTP isteği yapılmaz.
Gizli stok tükenme şablonu, açık satın alma düğmesinin yerine yorumlanmaz.

Runner denetimi: **Reviewed Budget Offer Recovery → public-source-audit**.
Gerçek katalogdaki iPhone 16 128 GB profilinde günlük keşif/okuma/kalite kodunu
çalıştırır; eski teklifler, aggregator veya tarayıcı çözücü kullanmaz. İki yeni
mağaza stokta doğrulanamazsa kırmızı olur. Üretim yazısı ve IndexNow yoktur;
sonuç ve kaynak erişim nedenleri `daily-price-report.json` artifact'ındadır.
Offline test: `python -m unittest -q test_public_retailer_sources`.

İlk gerçek runner sonucu ([25 Ekim denetimi](https://github.com/SalmanOz/products-scraper/actions/runs/40178126640)):
Troy'un iki canlı 128 GB renk bağlantısı doğrulandı; aynı mağaza oldukları için
yalnız **bir** mağaza sayıldı. PT yerelde doğrulanabildi, ancak runner'da
`robots.txt` 403 döndüğünden başka PT isteği yapılmadı. Dolayısıyla iki yeni
kaynağı birlikte doğrulama denetimi başarısızdır; PT'nin kesintisiz otomasyon
kapsamı sağladığı iddia edilmez. Bu engel kalite koşullarını düşürmek, IP/kimlik
değiştirmek veya eski fiyatı tazelemek için gerekçe değildir.

Android aday denetimi: **Reviewed Budget Offer Recovery → android-source-audit**.
Önce gerçek sekiz katalog profilinin mevcut PDP'lerini normal günlük güvenlik
koduyla GET-only yeniden okur. Ardından `android-source-candidates-2026-10-25.json`
içindeki 13 araştırma bağlantısını, kaynak başına robots ve en çok iki PDP
isteğiyle inceler. Robots gecikmesine uyar; HTML hata sayfası dönen HTTP 200
`robots.txt` izin kabul edilmez. Raporlar `daily-price-report.json` ve
`android-source-candidate-report.json` artifact'larıdır. Bu aday dosyası günlük
fiyat ipucu dosyası **değildir**; hiçbir aday sırf HTTP 200, InStock şeması veya
iki bağlantı bulundu diye onaylanmaz, teklifler üretime aktarılmaz. Sekiz
modelin ikişer geçerli mağazası yoksa denetim kırmızı kalır.

[25 Ekim Android runner sonucu](https://github.com/SalmanOz/products-scraper/actions/runs/40179012954):
73 offline test geçti; 8 modelin 5'inde yalnız idefix doğrulanabildi, A54,
Camon 40 ve Spark Slim 5G'de mevcut URL'lerden geçerli otomatik gözlem alınamadı.
Bu, piyasada stok olmadığı anlamına gelmez. 13 yeni adayın hiçbiri üretime
onaylanmadı; ikinci mağaza açığı 0/8 kapandı. Araştırma denetimi bu nedenle
başarısızdır; üretime teklif/ürün/şema yazılmadı. Yanlış bellek varyantı,
stokta olmayan ürün, konuma bağlı okunamayan görünür fiyat veya doğrulanmamış
doğrudan satıcı kimliği kalite kurallarını düşürerek kabul edilmedi.

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

## Fiyat yenileme / puan senkronizasyonu

Günlük fiyat keşfi Vatan'ın `robots.txt` içinde ilan ettiği ve taramaya izin
verdiği `sitemap.axd` dosyasını bir kez okur; yasaklı `/arama/*` yolunu kullanmaz.
Robots ve XML yanıtları boyut, URL sayısı ve yönlendirme sınırlarıyla denetlenir;
her model için en fazla dört tam model/RAM/depolama URL'si seçilir. Açık 4G/5G
çelişkisi elenir. Bu URL'ler yalnızca aday ürün sayfasıdır, fiyat veya stok kanıtı
değildir. Her adayın canlı Product→Offer kaydı, seçili varyantı, görünür peşin
fiyatı ve stok durumu mevcut kesin ürün sayfası doğrulamasından geçmelidir.
Engellenen ya da erişilemeyen sitemap fiyat üretmez. Çalışma raporundaki
`vatan_sitemap_discovery` alanı keşif durumunu ve uygun URL sayısını gösterir;
gerçek fiyat kapsamı ancak aday sayfalar doğrulandıktan sonra anlaşılır.

GitHub runner'da Vatan sitemap'i ve normal ürün GET'i 403 dönebildiği için
altı tam SKU bağlantısı ayrıca incelenmiş URL ipucu olarak tutulur. Bu kayıtlar
fiyat veya stok taşımaz. Her bağlantı için canlı robots izni, tam URL, seçili
Product→Offer, görünen peşin fiyat, satıcı, sepet ve stok yeniden doğrulanır;
yanıt veya doğrulama başarısızsa teklif üretilmez. Tek çalışmada en fazla altı
FlareSolverr GET yapılır; robots gecikmesine uyulur. Raporun
`vatan_reviewed_pdp` alanı robots durumu ve tarayıcı istek sayısını gösterir.
Son salt okunur runner pilotunda altı incelenmiş ürün sayfası 6/6 doğrulandı.

MediaMarkt'ın `robots.txt` içinde ilan ettiği iki ürün sitemap'i günlük işte
birer kez ve boyut sınırıyla okunur. On dört incelenmiş katalog varyantının tam
ürün URL'si ayrıca yalnız keşif ipucu olarak saklanır. GitHub runner normal
ürün GET'inde 403 alırsa, robots izinli ürün URL'si için mevcut FlareSolverr
üzerinden salt GET yapılır; tek çalışmada en fazla 16 tarayıcı isteği vardır.
Fiyat ancak seçili sayfadaki H1, canonical URL, SKU, tek Product/Offer, görünür
peşin fiyat, aktif sepet ve stok aynı varyanta bağlandığında alınır. Üçüncü
taraf pazar yeri satıcısı, yenilenmiş ürün, koşullu fiyat ve URL/varyant
çelişkisi reddedilir. `mediamarkt_source` raporu robots/sitemap ve tarayıcı
sonucunu ayırır. `MediaMarkt Reviewed SKU Price Refresh` manuel işi, günlük
fiyat yazarıyla aynı kuyruğu kullanarak yalnız bu 14 varyantı yeniler.

Pazarama keşfi, `robots.txt` izniyle Android telefon kategorisinin en fazla 16
yayınlanmış sayfasını bir kez okuyup çalışma boyunca önbelleğe alır. Kartlardan
yalnızca tam model, RAM ve depolama eşleşen ürün URL'leri alınır; kart fiyatı
kanıt değildir. Ürün sayfasında tek Product→Offer, H1, satıcı, yeni ürün/stok,
aktif “Sepete Ekle” ve görünür normal fiyat birlikte doğrulanır. “Sepette”
indirimi varsa JSON-LD'deki koşullu fiyat yalnız indirim etiketini doğrulamak
için kullanılır; kaydedilen fiyat ürünün ayrı, üzeri çizilmemiş normal fiyatıdır.
Kupon/üyelik fiyatı veya belirsiz fiyat düzeni elenir. Raporun
`pazarama_category_discovery` alanı keşif ve ürün sayfası istek sayılarını gösterir.

Günlük fiyat adımı başarılı olsa da kısmen başarısız olsa da ardından
`reconcile_scores.py` çalışır. Hiç yeni teklif bulunmaması, eski fiyat kanıtlarının
süresinin dolmadığı anlamına gelmez. Ayrıca `Reconcile Current Offer Scores`
saatlik olarak aynı denetimi yapar; kendi concurrency kuyruğunu kullanarak
bekleyen günlük fiyat işini iptal etmez. Fiyat yazımıyla tutarlılık sunucudaki
ürün satırı kilitleriyle sağlanır. GitHub kuyrukları nedeniyle kesin saat garantisi yoktur.
Salt-okunur audit modlarına hiçbir puan/teklif yazma adımı eklenmemiştir.

Bu işlem yalnız sunucudaki mevcut kanıtlardan tüm katalog yüzdeliğini tek
transaction içinde yeniden hesaplar. Mağazayı taramaz, fiyat veya gözlem tarihini
yenilemez. 72 saat, iki farklı güncel mağaza, varyant/para birimi/stok kontrolleri
ve doğrulanmış teknik puan gereklilikleri tam Teknoskor için aynıdır. İkinci kaynak
bulunamayan model doğrulanmış tam Teknoskor alamaz; eşleşen tek mağaza teklifi
varsa ayrı geçici puan alabilir. Bu durum iki mağaza açığını gidermiş gibi
raporlanmaz. Sonuç `score-reconciliation.json`
çıktısındadır; HTTP/hesaplama hatası iş akışını başarısız bırakır.

Dağıtım sırası: önce products deposundaki `/api/ingestion/scoring/reconcile`
uç noktasının yayına çıkmasını doğrulayın, sonra bu scraper sürümünü yayınlayın.
Aynı mevcut `SCRAPER_INGESTION_SECRET` kullanılır; anahtar veya DB erişimi
rapora yazılmaz. Elle onarım için Actions → Reconcile Current Offer Scores → Run
workflow kullanılabilir. Offline test: `python -m unittest -q test_score_reconciliation`.

## Öncelikli dört mağazada tüm katalog kurtarma

Actions → **Priority Retailer Full Catalog Recovery** mevcut yayımlanmış
kataloğu Hepsiburada, Trendyol, n11 ve Teknosa üzerinde tarar. `full` tüm mevcut
hedefleri yeniler. `coverage-first`, çalışmanın başında sunucudan alınan güncel
denetimde zaten en az iki bağımsız satıcısı ve güncel fiyatı olan modelleri
koruyup eksik fiyatlara ve ikinci satıcıya odaklanır; korunan modellerin mağaza
gözlem tarihleri yenilenmez. `publish=false` yalnız denetler; `publish=true`
aynı çalışmada tekrar okunan ve sunucu doğrulamasından geçen teklifleri yazar.
Bu akış yeni ürün veya teknik özellik oluşturmaz. Tüm modlar günlük fiyat
yazarıyla aynı concurrency kuyruğunu kullanır; yeni bir zamanlama oluşturmaz.

Yazım öncesindeki katalog veya ticari denetim gecikmesi bir teklifin gözlem
süresini aşırırsa yalnız o aday `expired_before_write` olarak atlanır; sonraki
aday kendi canlı kaynağından yeniden doğrulanır. Tazelik sınırları korunur.
Yazım başladıktan sonraki belirsiz sonuçlar tüm sonraki fiyat yazımlarını durdurur.

Kategori kartları yalnız ürün adresi ipucudur. Hepsiburada'da mağazanın kendi
bağlantılarıyla görülen marka/model kategorileri ve `rel=next` sayfaları en fazla
80 kategori okumasıyla izlenir. Aynı marka/model ve aynı sayfa numarasına taşınan
gözlenmiş kategori adresi bir kez ayrıca okunabilir; yönlenen belge fiyat kanıtı
olarak kullanılmaz. Trendyol kategori bütçesi 100, Teknosa bütçesi 40 sayfadır.
Kategori bütçeleri ürün sayfası bütçesinden ayrıdır; gecikme, robots ve kaynak
kesicisi her iki yolda da uygulanır. Ürün sayfasında kapasite, 4G/5G, seçili
ilan/satıcı, GTIN, stok ve normal veya açıkça ayrılmış sepette fiyat kanıtlanır.

Hepsiburada'nın aynı seçili ürün için oluşturduğu iki teknik tablo ancak tüm
metinleri birebir aynıysa tek gözlem olarak okunur. RAM, depolama, radyo,
SKU veya kondisyon dahil herhangi bir alan çelişirse ya da ikiden fazla kopya
varsa teklif reddedilir. Satıcı, seçili kontrol, barkod, stok ve fiyat
doğrulamaları ile yazım öncesi canlı okuma ayrıca zorunludur.

Teknosa'nın seçili pazaryeri ilanında RAM başlıkta yazmıyorsa, aynı ürün
kapsayıcısındaki tek teknik tablonun model, fiziksel RAM, depolama ve açık
5G desteği birlikte okunabilir. Beklenen katalog değerleri bu kanıta eklenmez.
Başlığın kendisi korunur; mevcut satıcı/SKU/GTIN/stok/fiyat kanıtı ve yeniden
canlı okuma geçmeden yazım yapılmaz. Eksik veya olumsuz 5G alanından 4G türetilmez.

`coverage-first` turu fiyat/ikinci satıcı açıklarını işledikten sonra, güncel
tüm-katalog denetiminde Teknosa teklifi bulunmayan çok satıcılı varyantları da
inceler. Yalnız mevcut katalog adresleri ve aynı turda gözlenen kategori
bağlantıları kullanılır; en fazla 2 adres/varyant, aynı mağaza istek bütçesi
ve kaynak kesicisi geçerlidir. Eski/eksik denetim bu ek kuyruğu açmaz.
`teknosa_store_gap_discovery` uygun ve incelenen varyantları ayrıca raporlar.

n11'in ürün URL'sinde Plus gibi bir model eki eksikse, aynı turdaki tam
seçili SKU/satıcı/GTIN/varyant kanıtı yalnız o adresin yeniden okunmasına
izin verebilir. Eski gözlem fiyat veya satıcı kaynağı olarak kullanılmaz.
İkinci okumada gerçek model, kapasite, stok, satıcı, GTIN veya fiyat değişirse
yazım yapılmaz; çelişen katalog sahipliği ve açık URL çelişkileri reddedilir.

Trendyol'da mevcut yenileme hedeflerinden en fazla sekiz farklı markanın ürünü
kategori keşfinden önce okunur; diğer katalog hedefleri normal turunda kalır.
Bu sayfaların gözlenen telefon/Android telefon marka kategorileri, zorunlu
genel kategori okumasından sonra aynı 100 sayfalık kuyruğa alınır. Marka kimliği
ve kategori başlığı tam eşleşir; sayfalama yalnız aynı kategori yolunda ilerler.
Marka numarası veya kategori adresi üretilmez. Tükenmiş bir ilanın kategori
bağlantısı keşif için kullanılabilir; teklif yazımı canlı seçili ürün ve satıcı
kanıtını ayrıca gerektirir. Kart fiyatları, eski satıcı fiyatları ve teknik
özellikler keşif üzerinden taşınmaz. `trendyol_brand_category_discovery` alanı
erkene alınan mevcut modelleri ve gözlenen kategori adreslerini raporlar.

`hepsiburada-category-audit` yalnız kategori yönlerini sınar: ürün sayfası
okumaz ve fiyat yazmaz. `n11-preflight` salt okunur kaynak tanısı içindir.
`target-preflight`, `target_slug` ile mevcut yayımlanmış ürünü ve `target_url`
ile dört mağazadan tek bir herkese açık ürün adresini alır. Güncel katalogdaki
özelliklere karşı normal tam seçili SKU ayrıştırıcısını çalıştırır; ürün,
özellik veya fiyat yazmaz. URL/model çelişkisi istekten önce, gerçek kapasite,
radyo, GTIN, satıcı ve stok çelişkileri ürün ayrıştırıcısında reddedilir.
Bu modda başarılı sonuç yalnız ayrıştırıcı kanıtıdır; yayımlama veya katalog
kapsamı başarısı değildir. Komut satırında `--target-preflight URL
--target-slug SLUG` kullanılır; `--publish` ve diğer tarama modlarıyla birleşmez.
Çıktılar `priority-catalog-sweep.json` ve `.md` dosyalarıdır. Kategori
olaylarında `parser_status`, gözlenen H1, güvenli yönlendirme ipucu ve HTTP
sonucu bulunur. `remaining_observed_pages` ve `category_budget_exhausted`,
bütçenin geride bıraktığı bağlantıları görünür kılar; bulunan adres sayısı fiyat
kapsamı sayılmaz. `other_memory_variant` tam doğrulanmış başka kapasitenin
önizlemesidir; özgün SKU'ya fiyat yazmaz ve kapsam yüzdesini artırmaz.

Tam taramalar gözlenen ama henüz okunmayan Hepsiburada kategori adreslerini
`.discovery-state/priority-hb-category.json` içinde korur. Sonraki tur kök
kategoriyi yeniden okur, ardından bu kuyruğu önce işler; aynı keşif çevriminde
daha önce doğrulanan kategori sayfaları kotayı yeniden tüketmez. Her turdaki
80 kategori ve ayrı ürün sayfası bütçesi değişmez. Kayıtta fiyat, mağaza/SKU
kanıtı veya kaynak HTML bulunmaz; teklifler yine canlı sayfalardan doğrulanır.
Kapasite/model kimliği değişirse veya keşif çevrimi altı saati geçerse kayıt
geçersizleşir. Kuyruk boşaldığında sonraki tur yeni bir keşif çevrimi başlatır.
Actions cache yalnız `full` ve `coverage-first` modlarında bu adres kaydını
taşır; katalog fiyat eşiği başarısız olsa da tamamlanan kategori ilerlemesi
korunur. Salt okunur tek-ilan ve kategori denetimleri bu kaydı kullanmaz.

Son kapsam eşiği geçilmezse Actions sonucu başarısız olabilir; daha önce
sunucuda doğrulanıp kabul edilen teklifler geri alınmış sayılmaz. `accepted`,
`public_coverage` ve kaynak ret nedenleri ayrı okunmalıdır. Birkaç teklifin
başarılı yayımlanması tüm katalog kalite eşiğinin geçtiği anlamına gelmez.

Offline kontroller iş akışındaki **Test full queue and live publication
boundaries** adımında listelenir.

## Scrapling Android salt-okunur pilotu

Actions → **Scrapling Android Read-only Pilot** → Run workflow. Yalnız manuel
çalışır; günlük fiyat işini veya canlı siteyi değiştirmez. İş akışına hiçbir
production secret verilmez, checkout kimlik bilgileri saklanmaz. Pilot kendisine
production yazma anahtarı verilirse çalışmayı reddeder. Tek Teknoskor erişimi,
8 ürünün kimlik/RAM/depolama eşleşmesini doğrulayan anonim public GET çağrılarıdır.

`requirements-scrapling-pilot.txt` ayrı ortamda kurulur. `scrapling_pilot.py`,
`budget-eight-2026-10-25.json` içindeki 8 model / 19 incelenmiş ürün URL'sini
requests, Scrapling HTTP ve Scrapling tarayıcı okuyucularıyla karşılaştırır.
Üçü de mevcut `_product_offer`, `rejection_reason` ve fiyat aykırılık kontrolünü
kullanır. Kaynak, mağaza veya eşik onayı genişletilmez. Model/radyo/RAM/depolama,
açık Türkiye garantisi, TRY peşin fiyat ve satın alınabilir stok doğrulanmalıdır.
Gözlemler bu çalışmadan ve en fazla 15 dakika eski olmalıdır; 72 saatlik üretim
eşiği değiştirilmez. Eski manuel kanıt dosyaları okunmaz.

Robots kuralları ve hız sınırları uygulanır. Eksik robots, 403/429 veya CAPTCHA
durumunda başka yöntemle engel aşılmaz. Tarayıcıda sadece GET, incelenen aynı
ana sayfa URL'si, gerekli public statik dosyalar ve aynı kökendeki izinli veri
istekleri açılır; POST, sepet/hesap/ödeme uçları, özel IP'ler, yeni sayfaya geçiş,
servis çalışanları ve indirmeler kapalıdır. Oturum açma, proxy, CAPTCHA çözme ve
adaptive fiyat eşleştirme yoktur. Bu nedenle erişim reddedilen hedeflerde
Scrapling'in engel aşma kapasitesi ölçülmez; rapor bunu açıkça belirtir.

`scrapling-pilot-report.json` her URL/yöntem için ret nedenini, gözlem tarihini,
yanıt hash'ini ve varsa doğrulanan teklifi içerir; çerez/başlık veya tüm sayfa
içeriğini saklamaz. `additional_model_merchant_pairs` aynı modele requests'e
göre eklenen farklı mağaza sayısıdır; renkler/ilanlar mağaza sayısını artırmaz.
Yöntemler fiyat, stok veya başlıkta çelişirse ek teklif kabul edilmez. İki
mağazaya yeni ulaşan model sayısı ayrıca raporlanır. Sonuç 0 ise 0 yazılır.
Hiçbir Scrapling hedefi denenememişse `comparison_status` "not_evaluated" olur;
işin tamamlanması kaynak başarısı olarak sunulmaz. Runtime hataları işi başarısız
bırakır. Tamamen engellenmiş kaynaklar ise geçerli ama kapsamı sınırlı rapordur.

Offline test: `python -m unittest -q test_scrapling_pilot test_retailer_offer_refresh
test_reviewed_offer_recovery`. Chromium kurulduktan sonra
`python -m unittest -q test_scrapling_pilot_runtime` dış ağa çıkmadan sentetik
sayfada gerçek JavaScript render eder. Canlı sonuçlar ancak tekrar çalışmaları
ve kanıt incelemesi sonrasında ayrı bir onayla günlük yazma akışına alınabilir.

## 🔗 Hızlı Bağlantılar

* **Ana Sayfa:** [Teknoskor.com](https://teknoskor.com)
* **Telefon Karşılaştırma:** [Teknoskor Telefon Karşılaştırma](https://teknoskor.com)
* **Akıllı Telefon Özellikleri:** [Teknoskor Cep Telefonları](https://teknoskor.com)

---
*Bu depo sadece Teknoskor altyapısında veri senkronizasyonu sağlamak amacıyla tasarlanmış yardımcı betikleri barındırır. Genel kullanıma veya kuruluma yönelik destek sunulmamaktadır.*
# Automatic memory variants

`sweep_priority_catalog.py --memory-variant-preflight` is a separate read-only
diagnostic. It derives other-capacity hints from the current authenticated
catalog, fetches the selected retailer listings, follows only observed Kimovil
model/variant links, and stages newly read technical facts for the actual
RAM/storage. It never uploads images or publishes a catalog row or price.

`--memory-variants` extends the full sweep with up to two other-capacity PDP
hints per existing model (24 per retailer) and prepares at most 12 separately
verified capacities. `--publish` enables the atomic `/api/ingestion/memory-variants`
writer and first-party image uploads. There are no hand-authored model/price
batches. Source denial circuits survive the variant stage. A technical page
must have the exact selected model, unambiguous physical RAM/storage,
radio, release and complete freshly sourced sections; a fresh live selected
retailer SKU/seller/stock/price is independently mandatory. Missing evidence
rejects that candidate without changing its parent. Original catalog identity
and coverage are reported separately from the enlarged catalog readback.

Teknosa memory discovery also accepts the ordinary reader's complete selected
physical specification table when its heading omits RAM. This table is parsed
without catalog input and must independently bind the full model, physical
RAM, storage and explicit 5G. Contradictory heading facts, missing/duplicate or
unrelated tables and incomplete selected seller/SKU/GTIN/stock/cash proof are
rejected. The preview only queues a separate variant: independent technical
verification, a fresh selected offer, atomic publication and exact readback
remain mandatory; the original capacity is never repriced from that preview.

Trendyol brand categories without pagination anchors can continue from their
public `__single-search-result__PROPS` category state. The heading, current
page index, total, current/next links, fixed public search parameters and exact
category path must agree. Only the ordinary `www.trendyol.com` HTML category
page is read next; the API URL is never requested, and serialized amounts,
sellers and account state never become routing or offer evidence. Every new
product address still needs the ordinary selected SKU/seller/stock/cash parser
and fresh publication read. All brands share the existing 100-page allowance
and source denial circuit.

Trendyol's current selected PDP may describe itself as `ProductGroup`. Its own
root SKU, group ID, exact canonical URL, name, brand and direct single Offer
must identify the requested PDP; separate RAM/storage/radio properties must
agree with the selected product attributes. Other `hasVariant` offers are
never selected, and a child pointing back to the current PDP, duplicate roots,
missing own Offer, aggregates or conflicting properties are rejected. All
ordinary merchant/SKU/GTIN/two-selected-variant-list/stock/cash/cart checks still
apply. The observed `12+256G` caption is interpreted like `12G+256G` only against
independently selected physical facts; server matching uses the same rule.
Raw headings are retained. Category captions remain routing hints only.

Teknosa's native technical tab lazily reads the public
`/getClassificationData?productCode=...` endpoint. For a fully selected
marketplace offer that still lacks physical RAM evidence, the reader can
fetch that tab only when the one native `#js-psh-btn`, selected cart and exact
PDP path agree on the product code and the own technical container is empty.
The request shares the same robots rules, denial circuit, delay and total
document allowance; a fresh variant recheck also spends its existing 24-read
allowance. A response is usable for at most 60 seconds and must provide one
explicit physical RAM, storage, positive 5G and complete matching model field
with their observed classification codes. Missing, ranged, duplicate,
conflicting, stale or other-product data cannot fill anything from catalog
expectations. Existing contradictory tables never fall back to this response.
The ordinary selected seller, SKU, GTIN, stock, cash/schema checks and later
fresh publication/readback remain required. The response contains no prices,
and neither changes catalog technical attributes nor creates products itself.

Hepsiburada's selected storage property can use `1 TB` while the catalog uses
1024 GB. Generic readers compare one exact GB/TB label numerically, then require
the visible selected control to retain that property's exact raw spelling.
TB technical evidence comes only from the own single native storage cell in
the selected table, with identical duplicate-table handling; ranges, mixed
amounts, extra storage rows, missing fields or a `Var` cell do not prove TB.
The same independent conversion feeds other-memory discovery. Raw titles,
schema prices, selected seller/SKU/GTIN/stock/radio proof and the separate
technical-document verification before new variant publication stay required.
The technical provider's native `12GB · 1TB` version label now ranks that
already-declared destination before another 12GB capacity. This only orders
the existing bounded reads; the selected page's full model, canonical/active
variant, RAM, storage and radio still have to pass independently. No capacity
slug or technical fact is synthesized from the label.

If that atomic publication loses its reply or returns HTTP 502/503/504, the
client can replay the identical transaction once. It reuses the already uploaded
images and fresh observations; it performs no extra retailer reads and never
retries an explicit quality/identity rejection. A replay may acknowledge a
committed new capacity as `existing`; it is counted as created only when its ID
was absent from the pre-attempt catalog, a second request was actually made,
and its expected slug still matches.
The full identity and exact commercial readbacks remain mandatory. Two uncertain
replies cannot count a variant as published, and an unacknowledged new catalog
row still stops the sweep. The artifact records the attempt count and reply
recovery separately from successful publication.

Families with no current priced capacity get an immediate preparation attempt
when a fully observed other-capacity preview is discovered. Each missing family
gets one early slot. Up to two families already known to have a current price
may also prepare one newly observed capacity immediately, before their live
source can become unavailable late in discovery. The other ten shared slots
stay reserved for unpriced families until discovery finishes; unknown public
price hints do not qualify for the covered-family allowance. A second capacity
of any early family and candidates beyond the allowance wait for the final pass.
Hepsiburada category-to-PDP routing shares that same two-family allowance with
preparation: it can read the observed new-capacity card before the next category,
even when no old product hint exists. An unverified card still consumes its
family's routing allowance, preventing unlimited speculative early PDP reads.
The artifact reports covered/unpriced early attempts separately; neither an
early slot nor a category card establishes a price or published capacity.
Early and final work share the same 12-candidate and 48-technical-request limits.
Publication rechecks share the discovery transport's robots, denial circuit,
cooldown and total request budget, with at most 24 fresh PDP rechecks per retailer.
Within one sweep, a browser-renderer timeout on an exact Hepsiburada SKU URL is
remembered only as a failed resource. Other seller/query aliases of that same
SKU are skipped without another request, including fresh rechecks. Parent
product URLs cannot establish this key. This avoids one slow SKU exhausting
the host's transient allowance twice. The existing cooldown, document limits,
explicit-denial circuit and second distinct-resource failure remain unchanged;
no price, stock or seller evidence is reused. Reports expose only the timed-out
SKU count and skipped-request count, and the record expires with the sweep.
Only deduplication is bypassed for an explicit fresh read. After any attempted
publication, unchanged original identities and acknowledged children are read
back before ordinary publishing continues. Created offers also require exact
public seller/URL/price readback; an unconfirmed commit or mismatch stops later
writes while GET-only diagnosis continues. The 80% gate uses the full enlarged
catalog, with the original catalog's coverage reported separately.

Variant readback compares every original authenticated technical attribute,
including nested sections and JSON value types, in addition to the complete
model/memory/radio identity. Changed prices and refresh URL hints are allowed;
changed original specifications stop subsequent publications. Reports expose
`parent_attributes_unchanged` separately from the identity count.

Rejected n11 other-memory previews also report their bounded source stage and,
only after independent extraction, the observed physical capacities/radio and
ordinary selected-reader rejection code. These diagnostics carry no prices,
account data or offer permission; they never become a publication candidate.

n11 PDP reads retain only the observed public `magaza`, `renk` and
`dahili-hafiza` selectors. The selected shop slug, color and capacity must
independently match the URL, public seller profile, SKU, GTIN, stock, serialized
cash/basket values and JSON-LD. Up to three actual same-product "Diğer Mağazalar"
links per catalog SKU can receive fresh reads, each followed by a separate
publication readback. Card prices never become offers. Invalid selectors and
private/tracking parameters remain rejected by both collector and ingestion.

Recovery mode also schedules the complete set of currently multi-seller SKUs
whose fresh whole-catalog audit contains no n11 offer, after all missing-price
and single-seller work. A missing or stale audit falls back to ordinary full
discovery. The existing per-source document allowance remains shared. Matching
family queries retain only a same-run document containing observed public card
names and URLs for other capacities, never prices, account state or retailer
SKU/stock evidence. Every resulting offer still requires its own selected PDP,
fresh publication read, exact listing writer and commercial readback.

Hepsiburada, Trendyol and Teknosa platform gaps are also recovered after all
primary missing-price/second-seller work and n11 discovery. Only a complete,
fresh whole-catalog audit can retain a multi-seller model and establish its
missing platform. Existing catalog URLs and at most 2,000 same-run category
URL/title/stock-priority hints per retailer supply at most two new reads per
model, preferring observed category destinations. No card amount, selected SKU
fact or old price is retained. Previously inspected/current URLs are excluded;
all existing source/PDP/category limits and stop rules remain shared. Every
offer still needs a fresh full selected read, atomic writer, acknowledged
scoring and exact commercial readback. Stale or missing inventories keep the
ordinary full-refresh fallback; they never open this supplementary queue.

Reports separately measure full-catalog Hepsiburada, Trendyol, n11 and Teknosa
coverage before and after the run using the fresh authenticated commercial
audit. Each platform counts a physical SKU once; valid listing rows remain
separate from independent seller counts. Missing, stale or partial audits are
reported as unavailable and never as zero coverage. These diagnostics do not
replace individual publication verification or the full public 80% gate.

The priority publisher avoids a second whole-catalog score recalculation only
when every committed ingestion batch explicitly confirms successful server
scoring. Missing, null, string or partial acknowledgements retain the separate
reconciliation. Exact current seller/URL/price readback remains mandatory even
when scoring is already confirmed.

Current commercial readback uses the authenticated
`/api/ingestion/offer-identity-readiness` endpoint and the same full commercial
matching audit. Historical price-index queries are not required to confirm a
newly accepted seller/URL/price. A missing or failed current audit still stops
later price writes; no cached result or successful empty audit substitutes it.

Technical documents use normal rendered GETs in the job's existing anonymous
browser service. Accepted URLs are limited to public Kimovil model pages and
its existing model autocomplete. Changed URLs, non-200 responses, challenge
documents and conflicting canonical/model/memory facts are rejected. A shared
model canonical requires its separately verified model page, the exact active
capacity link and the selected device metadata slug; it never supplies capacity.
No user
browser profile, authentication, proxy or cookie override is attached. Fetch
times survive in-memory reuse; offer timestamps are serialized as UTC without
renewing their observed time.

These modes are manual invocations in the existing serialized recovery
workflow. They add no schedule and do not relax the overall 80% price gate.

## Missing catalog radio from manufacturer specifications

The optional `resolve_radio` checkbox (default off) adds `--manufacturer-radio`
only to `--memory-variants` sweeps. It proves a missing radio before the sweep
takes its original catalog identity baseline. Do not run a separate metadata
writer during a live sweep: changing that baseline must fail verification.

The initial provider reads Infinix's public website specification mapping and
the exact specification ID observed there, under the site's robots rules and
a six-JSON-document limit (plus robots). It uses no account, cookies, redirects or retries after
a denial. The physical RAM/ROM cells and exact manufacturer model code must
match the existing catalog; extended RAM and Wi-Fi 5 GHz never establish these
facts. Only the explicit device `NETWORK / TECHNOLOGY` enumeration establishes
4G/5G. Other manufacturers remain unsupported rather than guessed.

The dedicated authenticated `/api/ingestion/catalog-radio` contract supports
read-only validation and atomic publication. It can add only the absent
`network_generation` scalar. It rechecks the full current model, model-code
alias, RAM, storage, existing radio and provenance under a product row lock.
Known values and every other attribute remain unchanged, including the old
Connectivity group's source hash. A matching committed observation can be
retried after a scoring failure; a different known radio cannot be replaced.

`catalog-radio-resolution.json` records observations, server validation and
whole-catalog attribute readback. A failed/unconfirmed publication stops before
any price sweep begins. Manufacturer data never supplies a retailer price,
seller or stock state; those still need the ordinary fresh selected-PDP proof,
publication read and exact commercial readback. The catalog size and 80% gate
are unchanged by this optional phase.

