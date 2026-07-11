# Voice/Speech Activity Detection Comparison Application

Farklı VAD (voice/speech activity detection) yöntemlerini **plugin** olarak çalıştırıp
aynı ses üzerinde **görsel olarak karşılaştıran** uygulama.

Softphone istemcisi konuşmayı **gerçek SIP + RTP** (G.711 PCMU, 8 kHz, 20 ms)
ile server'a gönderir — unimrcp'nin medya yoluyla birebir aynı koşullar.
Server etkin tüm VAD motorlarını aynı ses üzerinde paralel çalıştırır ve web
önyüzünde zaman ekseninde gösterir: dalga formu, her motorun skor eğrisi ve
yakaladığı konuşma bölgeleri, elle işaretlenen "gerçek konuşma" (ground truth)
şeridi ve motor başına precision/recall/F1 tablosu.

## VAD motorları

| Motor | Yöntem | Kaynak |
|---|---|---|
| `unimrcp_vad` | Enerji tabanlı 4-durumlu state machine — **gerçek UniMRCP C kodu** (`mpf_activity_detector.c`), derlenip ctypes ile sarılır | [unimrcp](https://github.com/unispeech/unimrcp), Apache 2.0 |
| `silero_vad` | Nöral model, ONNX v5, onnxruntime | [snakers4/silero-vad](https://github.com/snakers4/silero-vad) |
| `ten_vad` | Nöral model, prebuilt lib (pip) | [TEN-framework/ten-vad](https://github.com/TEN-framework/ten-vad) |
| `arf_vad` | Adaptif gürültü tabanı (asimetrik one-pole) + SNR onset/offset histerezisi + DC blocker + ZCR frikatif desteği + libfvad spektral kapısı — **arf-recog-adaptive-vad plugin'inin gerçek C kodu** (`arf_vad.c`), derlenip ctypes ile sarılır | UniMRCP plugin (yerel) + [libfvad](https://github.com/dpirch/libfvad), BSD-3 |

`arf_vad`, plugin'deki dağıtım davranışını birebir taşır: libfvad (WebRTC VAD)
her 10 ms karede oy verir, son `fvad_window` karenin konuşma oranı konuşma
öncesi `fvad_open_pct` / konuşma içinde `fvad_hold_pct` eşiğiyle karşılaştırılır
ve gürültü oyu onset/offset'i veto eder (rüzgar/klik kapısı). Kısa ve yüksek
kelimelerin veto edilmesine karşı `spec_bypass_snr` kolu, `use_fvad=0` ile
salt enerji/SNR kipi denenebilir; proximity kapıları (`onset_level`,
`dominant_drop_db`, `adaptive_margin_db`) varsayılan olarak kapalıdır.

Yeni motor eklemek: `server/vad/engines/` altına `VadEngine` türevi bir dosya,
`server/vad/registry.py` listesine bir satır.

## Kurulum

Gereksinimler: macOS, `clang`/`make` (Xcode CLT), [uv](https://docs.astral.sh/uv/)
(`brew install uv`). Python 3.12 uv tarafından otomatik kurulur (`.python-version`).

```sh
make setup     # C kütüphanesi + bağımlılıklar + Silero ONNX modeli
make test      # birim testleri
```

## Kullanım

```sh
make run     # her şeyi başlatır (eskiyi kendisi kapatır)
make stop    # çalışan her şeyi durdurur
make status  # neyin çalıştığını gösterir
```

Sonra tarayıcıda **tek adres**: `http://127.0.0.1:8080`

1. Sağ üstteki **🎤 Record** düğmesine basın, konuşun, **■ Stop** ile durdurun.
   (Düğme, arka planda softphone istemcisine gerçek bir SIP çağrısı açtırır —
   ses RTP ile server'a akar, dört VAD motoru canlı çalışır.) Kayıt yerine bir
   ses dosyası denemek için **📁 WAV file…** düğmesini kullanın.
2. Grafik konuşurken canlı dolar; durdurunca kayıt oturum olarak saklanır ve
   otomatik açılır: waveform + motor başına skor eğrisi ve segmentler.
3. Sağ paneldeki motor kartlarından motorları açıp kapatın ve parametreleri
   değiştirin. Bir oturum açıkken kart düğmesi **Re-analyze recording** olur:
   parametreyi o kaydın üzerine anında (offline) uygular. **Re-analyze all**
   tüm etkin motorları yeniden çalıştırır. (Oturum yokken parametreler bir
   sonraki canlı çağrıda geçerli olur.)
4. **Annotate** ile ground-truth (gerçek konuşma) bölgelerini işaretleyin
   (sürükle = oluştur, kenar = boyutlandır, gövde = taşı, çift tık = sil),
   **Save annotations** ile kaydedin — sağ panelde precision/recall/F1
   tablosu belirir.

> Not: Uygulama aslında iki süreçten oluşur (server + softphone istemcisi),
> çünkü gerçek bir SIP çağrısı simüle ediliyor. Ama bu bir ayrıntıdır: server
> istemciyi kendisi başlatır, siz yalnızca **8080**'i kullanırsınız.
> `http://127.0.0.1:8081` istemcinin kendi paneli olup normalde gerekmez.
> İleri kullanım için süreçleri ayrı başlatmak isterseniz: `vad-server
> --no-client` ve `make run-client`.

### Dosya ile tek seferlik çağrı (UI'sız)

```sh
uv run vad-client --wav tests/fixtures/speech.wav --no-ui
```

### Offline analiz (ağ olmadan)

```sh
uv run python -m cli.analyze tests/fixtures/speech.wav --engines all
uv run python -m cli.analyze kayit.wav --engines silero_vad --param silero_vad.threshold=0.6 --json
```

## Mimari

```
client (softphone)                    server
┌─────────────────────┐   SIP/UDP    ┌──────────────────────────────────┐
│ mic 48k → soxr 8k ─┐ │  INVITE/    │ SIP UAS → çağrı başına RTP portu │
│ WAV → 8k ──────────┤ │  ACK/BYE    │ RTP → reorder → μ-law decode     │
│ 20ms frame → μ-law ├─┼─────────────┤  ├─ audio.wav kaydı              │
│ RTP/UDP ───────────┘ │  RTP/UDP    │  ├─ peaks (waveform)             │
│ yerel web UI :8081   │   PCMU      │  └─ her motor: EngineRunner      │
└─────────────────────┘              │     (resample+rebuffer+segment)  │
                                     │ WS + REST → web UI :8080         │
                                     └──────────────────────────────────┘
```

- **Zaman ekseni sözleşmesi**: tüm zamanlar 8 kHz akışın örnek sayacından türetilir;
  kayıp RTP paketleri sessizlikle doldurulur, eksen asla kaymaz.
- **Backdating**: dedektörler konuşma başlangıcını ancak `speech_timeout` sonra
  doğrular; olaylar geçmişe dönük zaman damgası taşır, segmentler gerçek
  başlangıçtan çizilir.
- Oturumlar `data/sessions/<zaman>_<id>/` altında: `audio.wav`, `session.json`
  (segmentler/olaylar/skorlar), `annotations.json` (ground truth, ayrı dosya).

## Bilinen notlar

- **Mikrofon izni (macOS)**: ilk çalıştırmada System Settings → Privacy &
  Security → Microphone altında terminal uygulamanıza izin verin. WAV modu
  izinden bağımsız çalışır.
- `ten-vad` paketi kurulamazsa uygulama 2 motorla çalışır; motor kartında
  "unavailable" nedeni gösterilir.
- Portlar: SIP udp/5060, web 8080/8081, RTP 40000–40019 — hepsi CLI
  bayraklarıyla değiştirilebilir (`--help`).

## Lisanslar

- `third_party/unimrcp_vad/`: UniMRCP'den türetilmiştir — Apache 2.0
  (bkz. LICENSE ve NOTICE).
- `third_party/arf_vad/`: arf-recog-adaptive-vad UniMRCP plugin'inden mekanik
  olarak çıkarılmıştır (bkz. NOTICE; APR/APT/MPF bağımlılıkları giderildi,
  algoritma değişmedi).
- `third_party/libfvad/`: WebRTC türevi [libfvad](https://github.com/dpirch/libfvad)
  — BSD 3-clause (bkz. LICENSE ve NOTICE).
- Silero VAD modeli: MIT. TEN VAD: Apache 2.0 + ek koşullar (kendi reposuna bakın).
