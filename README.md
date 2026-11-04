# Dedplay Film

Fotoğraf ve video arşivini, uğraşmadan kaliteli bir filme çeviren ev sunucusu uygulaması.
"2025" de, süreyi seç, gerisini uygulama yapar.

## Ne yapar?

1. **Arşivi tarar.** Fotoğraf ve video klasörlerindeki her dosyanın çekim tarihini (EXIF, Google Takeout .json, dosya adı) ve konumunu okur, sonra her fotoğrafın netliğini ölçer.
2. **En iyi kareleri seçer.** Art arda çekilmiş benzer kareleri, bulanık fotoğrafları, ekran görüntülerini ve iPhone Live Photo videolarını eler. Seçilen süreye göre zaman çizgisinden dengeli seçim yapar.
3. **Bölümler.** Kısa aralıkları günlere ("1. Gün · 12 Temmuz 2025, Cumartesi"), uzun aralıkları aylara ayırır.
4. **Kurgular:**
   - Fotoğraflara Ken Burns hareketi (yavaş zoom ve kaydırma) verir.
   - Dikey fotoğraf ve videoların arka planını bulanık doldurur.
   - Uzun videolardan kesit alır.
   - iPhone HDR videolarının renklerini düzeltir.
   - Açılış, bölüm ve kapanış başlıkları ekler.
5. **Ses:** Müzik sırayla ve geçişli çalar. Videoda konuşma olduğunda müzik otomatik kısılır. Videoların sesi eşitlenir ve uğultu filtrelenir.
6. **Çıktı:** 1080p MP4 dosyası, `/DATA/Media/Movies/Dedplay Film` klasörüne yazılır.

Tamamen ücretsiz, açık kaynak araçlarla (FFmpeg, ExifTool, Pillow) çalışır. Dışarıya hiçbir veri gönderilmez.

## Kurulum (dedplay sunucusu)

```bash
cd /DATA/AppData
git clone https://github.com/berzahbey/dedplay-film.git dedplay-film-src
cd dedplay-film-src
mkdir -p "/DATA/Media/Movies/Dedplay Film" /DATA/AppData/dedplay-film
docker compose up -d --build
```

Uygulama şu adreste açılır: `http://<sunucu-ip>:8090`

Güncellemek için:

```bash
cd /DATA/AppData/dedplay-film-src && git pull && docker compose up -d --build
```

## Klasörler

| Sunucuda | Container'da | Not |
|---|---|---|
| /DATA/Media/Photos | /media/photos | salt okunur |
| /DATA/Media/video | /media/videos | salt okunur |
| /DATA/Media/Music/music | /media/music | salt okunur, varsayılan klasör: `muzaffer muzik` |
| /DATA/Media/Movies/Dedplay Film | /output | bitmiş filmler |
| /DATA/AppData/dedplay-film | /app/data | veritabanı, küçük resimler, geçici dosyalar |

## İlk kullanım

1. **Arşivi tara**'ya bas. İlk tarama büyük bir arşivde birkaç saat sürebilir. Sonraki taramalar sadece yeni dosyalara bakar.
2. Bir yıl (veya tarih aralığı ya da klasör) seç, film süresini seç, sonra **Kareleri seç**'e bas.
3. Önizlemede istemediğin karelere tıklayıp çıkar.
4. Müzikleri işaretle ve **Filmi oluştur**'a bas.

## Yol haritası

- v2: Video titreme giderme (vidstab), yapay zekâ ile rüzgar ve gürültü temizliği (DeepFilterNet), cihaz saat düzeltme
- v3: Müzik ritmine göre kurgu, yüz tanıyarak kırpma, GPS'ten yer adları
- v4: Yerel yapay zekâ ile başlık ve hikâye, seslendirme, dikey Reels özeti
