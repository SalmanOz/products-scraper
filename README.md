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
Günlük zamanlama fiyatları günceller; kendiliğinden model yayımlamaz.

## 🔗 Hızlı Bağlantılar

* **Ana Sayfa:** [Teknoskor.com](https://teknoskor.com)
* **Telefon Karşılaştırma:** [Teknoskor Telefon Karşılaştırma](https://teknoskor.com)
* **Akıllı Telefon Özellikleri:** [Teknoskor Cep Telefonları](https://teknoskor.com)

---
*Bu depo sadece Teknoskor altyapısında veri senkronizasyonu sağlamak amacıyla tasarlanmış yardımcı betikleri barındırır. Genel kullanıma veya kuruluma yönelik destek sunulmamaktadır.*
