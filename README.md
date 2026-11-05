# Dedplay Arşiv — Kopya Temizleyici ve Düzenleyici

Fotoğraf ve video arşivini tarar, kopyaları ayıklar ve her şeyi düzenli klasörlere yerleştirir.

- **Kopyalar:** Birebir aynı dosyalar; farklı ad, boyut ya da sıkıştırmadaki aynı görüntüler (WhatsApp, Google Fotoğraflar kopyaları, yeniden kaydedilmiş videolar); seri çekimler. Her grupta **en iyisi** kalır: gerçek çekim tarihi, en yüksek çözünürlük, en net, gözleri açık olan. Diğerleri **çöp kutusuna taşınır**, silinmez.
- **Düzen:** `Resimler/<yıl>`, `Videolar/<yıl>`. Ekran görüntüleri, ekran kayıtları ve WhatsApp/Instagram/TikTok/TV gibi başka yerlerden kaydedilenler `Ekran ve Kaydedilenler/Resimler|Videolar/<yıl>` klasörüne gider. Tarihi bilinmeyenler `Tarihsiz` klasörüne konur.
- **Korunan klasörler** (ör. `video/kenan dugun`, `video/Necla video`, `Photos/muzaffer resim`) olduğu gibi kalır. İçlerindeki dosyalar taşınmaz ve asla çöpe atılmaz.
- **Gelen kutusu:** Yeni dosyaları `/DATA/Media/Gelen` klasörüne at. 5 dakikada bir işlenir: kopyaysa çöpe, değilse yerine. Ayrıca 6 saatte bir tüm arşiv kontrol edilir.
- **Çöp kutusu:** `/DATA/Media/Dedplay Çöp Kutusu`. Klasör yapısı korunur. Tek tek, seçerek ya da hepsini geri alabilir veya kalıcı silebilirsin. Geri alınan dosya bir daha çöpe atılmaz.
- **Geri alma:** Her düzenleme turu kaydedilir. "Son düzenlemeler" bölümünden dosyalar eski klasörlerine geri döndürülebilir.
- **Sekmeler:** Tüm resimler, Tüm videolar, Ekran & kaydedilenler (yıla göre galeri, büyük görüntüleme), Çöp kutusu, Ayarlar.

Eski film bölümü `/film` adresinde duruyor (deneysel).

---

## (Eski) Dedplay Film

Fotoğraf ve video arşivini, uğraşmadan kaliteli bir filme çeviren ev sunucusu uygulaması.
"2025" de, süreyi seç, gerisini uygulama yapar.

## Ne yapar?

1. **Arşivi tarar.** Her dosyanın çekim tarihini (EXIF, Google Takeout .json, dosya adı) ve konumunu okur, sonra netliğini ve görsel izini çıkarır. En yeni dosyalardan başlar. Tarama sürerken film yapılabilir.
2. **Kopyaları ayıklar.** Adı, boyutu ya da sıkıştırması farklı olsa da görüntüsü aynı olan fotoğraf ve videolar (WhatsApp kopyaları, Google Fotoğraflar yedekleri, yeniden kaydedilmiş videolar) filme bir kez girer. Her grupta en iyi kopya kalır: gerçek çekim tarihi olan ve en yüksek çözünürlüklü olan.
3. **En iyi kareleri seçer.** Art arda çekilmiş benzer kareleri, bulanık fotoğrafları, ekran görüntülerini ve Live Photo videolarını eler. Seçilen süreye göre dönem boyunca dengeli seçim yapar.
4. **Uzun videolarda en iyi anları bulur.** Ses canlılığı (konuşma, gülüşme), hareket, netlik ve aydınlığa bakar. Video boyunca birbirinden uzak 1-6 kesit seçer ve kesme noktalarını sessiz anlara kaydırır.
5. **Sahne kurgusu yapar.** Bir video çekilirken ya da hemen öncesinde/sonrasında aynı rotada çekilen fotoğraflar o videoya "ara plan" olur: video oynarken ekrana gelir, videonun sesi kesilmez. Art arda çekilmiş iki dikey fotoğraf yan yana tek karede gösterilir.
6. **Efektler:**
   - Ken Burns hareketi
   - Geçiş efektleri
   - Müziğin ritmine göre kesme
   - Renk stilleri: Sade, Sinematik, Enerjik
   - İsteğe bağlı sinema bantları
   - Titreyen videolar sabitlenir (vidstab), fotoğraf hareketleri alt-piksel hassasiyetle titremesiz
   - Yan/ters kaydedilmiş videolar: yüzlere bakılarak otomatik ya da önizlemede ↻ ile elle döndürme
   - Dikey, kare ve 4:3 videolar tam ekran: yüzü ve hareketi takip eden akıllı kırpma, kameraman gibi yumuşak hareket
   - iPhone HDR renk düzeltmesi
   - Açılış, bölüm ve kapanış başlıkları
7. **Ses:** Müzik sırayla ve geçişli çalar. Videoda konuşma olduğunda müzik otomatik kısılır. Videoların sesi eşitlenir ve uğultu filtrelenir.
8. **Çıktı:** 1080p MP4 dosyası, `/DATA/Media/Movies/Dedplay Film` klasörüne yazılır.

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

- v3: Video titreme giderme (vidstab), yapay zekâ ile rüzgar ve gürültü temizliği (DeepFilterNet), cihaz saat düzeltme, yüz tanıyarak kırpma, GPS'ten yer adları
- v4: Yerel yapay zekâ ile başlık ve hikâye, seslendirme, dikey Reels özeti
