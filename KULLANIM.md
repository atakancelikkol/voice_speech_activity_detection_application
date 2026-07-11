# Kullanım Notu

Kısa ve pratik başvuru. Ayrıntılı açıklama için [README.md](README.md).

## Başlat / Durdur (terminal)

```sh
make run      # HER ŞEYİ başlatır (server + softphone istemcisini kendisi açar)
make stop     # çalışan her şeyi durdurur, portları serbest bırakır
make status   # neyin çalıştığını gösterir
```

- **Yeniden başlatmak** için tek komut yeterli: `make run` (eskiyi kendisi kapatır).
- **İlk kurulum** (bir kereye mahsus): `make setup` — C kütüphanelerini derler,
  bağımlılıkları kurar, Silero modelini ve gürültü kayıtlarını indirir.
- Durdurmak için terminalde `Ctrl-C` ya da başka bir terminalde `make stop`.

---

## 🌐 8080 — Kullanacağınız tek adres

Tarayıcıda açın: **http://127.0.0.1:8080**

Burada her şeyi yaparsınız. Sağ üstteki iki düğme:

| Düğme | Ne yapar |
|---|---|
| 🎤 **Record** | Mikrofondan kaydeder. Basın → konuşun → **■ Stop**. Kayıt boyunca 4 VAD motoru canlı çalışır; durdurunca sonuç otomatik açılır. |
| 📁 **WAV file…** | Bu makinedeki bir ses dosyasını analiz eder (yol sorar, örn. `tests/fixtures/noisy_snr5.wav`). Kayıt yapmadan denemek için. |

Sonuç grafiğinde (zaman ekseni):
- **waveform** — ham dalga formu
- **unimrcp / silero / ten / arf** — her motorun yakaladığı konuşma bölgeleri + skor eğrisi
- **ground truth** — sizin elle işaretlediğiniz "gerçek konuşma"

Sağ paneldeki motor kartları:
- Onay kutusuyla motoru **aç/kapa** (kapalı motor bir sonraki çağrıda çalışmaz).
- Parametreleri değiştirin (ör. unimrcp `Level threshold`).
- Bir kayıt açıkken düğme **"Re-analyze recording"** olur: parametreyi o kaydın
  üzerine **anında (offline)** uygular — yeni çağrı yapmanıza gerek yok.
  Üstteki **"Re-analyze all"** tüm motorları birden yeniden çalıştırır.

Diğer araçlar:
- **Annotate** — timeline'da "gerçek konuşma" bölgelerini işaretleyin
  (sürükle = oluştur, kenar = boyutlandır, gövde = taşı, çift tık = sil),
  **Save annotations** ile kaydedin. Sağ altta precision/recall/F1 tablosu çıkar.
- **Fit / Follow live** — grafiği sığdır / canlı çağrıda sağ kenarı takip et.
- Soldaki **Sessions** listesi — geçmiş kayıtları açar (ses çalar, playhead senkron).

---

## 🔒 8081 — Buraya girmenize GEREK YOK

`http://127.0.0.1:8081`, softphone istemcisinin **iç servisidir** — gerçek SIP
çağrısını yapan parça. **Kendi arayüzü yoktur.** Server onu arka planda kendisi
başlatır; siz yalnızca 8080'i kullanırsınız.

Yanlışlıkla 8081'e girerseniz "Nothing to see here" yazan, sizi otomatik olarak
8080'e yönlendiren bir bilgi sayfası görürsünüz. Yani orada yapılacak bir şey yok.

**Neden var?** Uygulama, unimrcp'nin ses yolunu birebir taklit etmek için gerçek
bir SIP+RTP çağrısı kuruyor: 8081'deki istemci arayan (UAC), 8080'deki server
arananan (UAS). Bu bir mimari ayrıntıdır, günlük kullanımda görünmez.

**İleri kullanım** (nadiren gerekir): iki süreci ayrı terminallerde çalıştırmak
isterseniz —
```sh
vad-server --no-client   # server (istemciyi kendisi başlatmaz)
make run-client          # softphone istemcisini ayrı başlat
```

---

## Testler

```sh
make test     # tüm testler (66 test: birim + uçtan uca SIP + gürültü dayanıklılığı)
```

Gürültülü test dosyalarını yeniden üretmek / gerçek gürültü indirmek:
```sh
make noise    # MS-SNSD gerçek ortam gürültüsünü indirir (data/noise/)
make wavs     # temiz + gürültülü (15/5 dB SNR) test WAV'larını üretir
```

Ağ olmadan hızlı analiz (terminalden):
```sh
uv run python -m cli.analyze tests/fixtures/noisy_snr5.wav --engines all
```

---

## Notlar

- **Mikrofon izni (macOS):** ilk kayıtta terminale mikrofon izni sorabilir
  (System Settings → Privacy & Security → Microphone). WAV dosyası modu izinden
  bağımsız çalışır.
- **Portlar:** 8080 (web), 8081 (softphone iç servisi), 5060/udp (SIP),
  40000–40019/udp (RTP). Hepsi `vad-server --help` ile değiştirilebilir.
- **"Port kullanımda" hatası:** başka bir kopya çalışıyordur — `make stop` deyip
  tekrar `make run`.
