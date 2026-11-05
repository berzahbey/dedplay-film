# Dedplay Arşiv

Ev sunucusundaki (ZimaOS) fotoğraf ve video arşivini tarar, kopyaları ayıklar ve her şeyi yıllara göre düzenli klasörlere yerleştirir. iPhone yedeklerini kendiliğinden alıp işler. Yüz tanıma ile kişilere göre de gezinebilirsin.

## Ne yapar?

- **Kopyalar:** Birebir aynı dosyalar; adı, boyutu ya da sıkıştırması farklı ama görüntüsü aynı olanlar (WhatsApp, Google Fotoğraflar kopyaları, yeniden kaydedilmiş videolar); seri çekimler; aynı sahne ve aynı kişilerle çekilmiş benzer kareler. Her grupta **en iyisi** kalır (gerçek çekim tarihi, yüksek çözünürlük, netlik, açık gözler, gülümseme). Diğerleri **çöp kutusuna** gider; hiçbir şey kendiliğinden kalıcı silinmez.
- **Düzen:**
  ```
  /DATA/Media/
  ├─ Photos/<yıl>/                       tüm fotoğraflar
  ├─ video/<yıl>/                        tüm videolar
  └─ Arsiv/
     ├─ Gelen/                           yeni dosyalar (otomatik işlenir)
     ├─ Ekran ve Kaydedilenler/Resimler|Videolar/<yıl>
     └─ Dedplay Cop Kutusu/              çöp kutusu (taranmaz)
  ```
  Çekim tarihi bilinmeyenler `Tarihsiz` klasörüne gider. Canlı fotoğrafların (Live Photo) videoları fotoğrafın yanında kalır.
- **Korunan klasörler** (Ayarlar'dan seçilir) olduğu gibi kalır: içlerindeki dosyalar taşınmaz, çöpe atılmaz.
- **Gelen kutusu:** `Arsiv/Gelen` 5 dakikada bir işlenir: arşivde zaten varsa çöpe ("zaten arşivde"), daha kalitelisiyse arşivdekinin yerine geçer, yeniyse yerine yerleşir. 6 saatte bir tüm arşiv de kontrol edilir.
- **iPhone yedeği:** ZimaOS istemcisinin yedeklediği klasör (ör. `/DATA/Backup/iPhone 14 Plus`) her dakika kontrol edilir; yüklemesi biten dosyalar Gelen'e alınıp işlenir.
- **Kişiler:** Yüzler bulunur ve kişilere ayrılır. İsim ver, aynı kişinin kartlarını birleştir (aynı ismi vermek de birleştirir), yanlış fotoğrafları "Bu kişi olmayanları seç" ile çıkar.
- **Çöp kutusu:** Her tur kendi klasöründe ("2026-11-05 14.30 Gelen"). Tek tek, seçerek ya da hepsini geri al / kalıcı sil.
- **Geri alma:** Her düzenleme turu kaydedilir; "Son düzenlemeler"den dosyalar eski yerlerine döndürülebilir.
- **Galeri:** Tüm resimler, Tüm videolar, Ekran & kaydedilenler — yeniden eskiye, sağda yıllar, büyük görüntüleme.

---

## Kurulum

İki yol var. **A** en kolayıdır (ZimaOS arayüzünden, terminal gerekmez). **B** kaynak koddan derler.

### A) ZimaOS arayüzünden (hazır imaj)

1. ZimaOS → **App Store** → sağ üstte **+** → **Install a customized app** (Özel uygulama yükle) → **Import** (İçe aktar).
2. Repodaki [`zimaos.yaml`](zimaos.yaml) dosyasının içeriğini yapıştır.
3. Gerekirse klasörleri değiştir (aşağıdaki "Ayarlanacaklar"), **Install**.
4. Tarayıcıda `http://<sunucu-ip>:8090` aç.

> İmaj GitHub'da her push'ta otomatik derlenir: `ghcr.io/berzahbey/dedplay-film:latest` (Actions sekmesinden izlenir).
> İlk derlemeden sonra GitHub'da bir kez **Profil → Packages → dedplay-film → Package settings → Change visibility → Public**
> yap; yoksa ZimaOS imajı indiremez. Güncellemek için: `docker compose pull && docker compose up -d` ya da ZimaOS'ta uygulamayı yeniden kur.

### B) Terminalden, kaynak koddan

```bash
mkdir -p /DATA/AppData && cd /DATA/AppData
git clone https://github.com/berzahbey/dedplay-film.git dedplay-film-src
cd dedplay-film-src
# gerekirse docker-compose.yml içindeki klasörleri düzenle
docker compose up -d --build
```

İlk derleme 10-15 dk sürebilir (yüz tanıma modeli ~100 MB indirilir).

**Güncelleme:**
```bash
cd /DATA/AppData/dedplay-film-src && git pull && docker compose up -d --build
```

---

## Ayarlanacaklar

| Ne | Nerede | Varsayılan |
|---|---|---|
| Arşiv (fotoğraf/video ana klasörü) | `volumes: - /DATA/Media:/archive` | `/DATA/Media` |
| Taranan klasörler | `MEDIA_DIRS` | `Photos, video, Arsiv` |
| Çöp kutusu | `TRASH_DIR` | `/archive/Arsiv/Dedplay Cop Kutusu` |
| iPhone yedek klasörü | `volumes: - /DATA/Backup:/backup` ve `INBOX_FEEDS` | `/backup/iPhone 14 Plus` |
| Veritabanı, ayarlar, küçük resimler | `volumes: - /DATA/AppData/dedplay-film:/app/data` | |
| Port | `ports: - "8090:8000"` | `8090` |

- Telefonunun adı farklıysa `INBOX_FEEDS` içindeki `iPhone 14 Plus` kısmını `/DATA/Backup` altındaki klasör adıyla değiştir. Birden fazla telefon için virgülle ayır: `INBOX_FEEDS=/backup/iPhone 14 Plus,/backup/iPhone 13`.
- iPhone yedeği kullanmıyorsan `INBOX_FEEDS` satırını ve `/DATA/Backup` bağlantısını silebilirsin.
- Korunacak klasörler, canlı fotoğraf ve kısa video davranışı, otomatik düzen: uygulamada **⚙ Ayarlar**.
- Klasör adlarında Türkçe karakter kullanma (ş, ç, ö…): ZimaOS istemcisi bunları seçemeyebilir.

## İlk kullanım

1. **Temizle & Düzenle → 1 · Kopyaları bul.** Tüm arşiv taranır (büyük arşivde ilk tarama birkaç saat sürebilir; sayfayı kapatabilirsin).
2. Bitince **Onayla**: kopyalar çöpe, kalanlar yerine. Bundan sonra **otomatik düzen** kendiliğinden açılır.
3. Yüz taraması arka planda kendiliğinden başlar; **👤 Kişiler** sekmesi dolar.
4. **Çöp kutusu**'na göz at; istediğini geri al, gerisini kalıcı sil.

## Sorun giderme

```bash
docker logs dedplay-film --tail 50                     # hata var mı
docker logs dedplay-film 2>&1 | grep -i besleyen       # iPhone yedeğinden alınan dosyalar
curl -s localhost:8090/api/clean/status | head -c 400  # şu anki iş
```

- **ZimaOS istemcisinde klasör seçilemiyor:** istemcinin iPhone yedeği yalnızca kendi `Backup` klasörüne gider ve sonradan değiştirilemez; uygulama o klasörü zaten izliyor, seçmene gerek yok.
- **Dosyalar Tarihsiz'e gidiyor:** dosya çekim tarihini kaybetmiş (bazı paylaşım yolları EXIF'i siler). Orijinal dosyayı gönder (AirDrop, ZimaOS yedeği, kabloyla).

Eski film bölümü `/film` adresinde duruyor (deneysel).
