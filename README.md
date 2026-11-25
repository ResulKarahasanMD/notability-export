# notability-export

Converts Notability notes (`.nbn` packages in iCloud Drive) into app-independent files: a PDF with the
original slides and the handwriting drawn on top, a Markdown file with the text, and the audio recordings.
Resumable batch runner, read-only on the source notes, macOS only. The rest of this README is in Turkish.
Not affiliated with Notability or Ginger Labs; the file format was worked out from the author's own notes.

iCloud'daki Notability notlarını (`.nbn` paketleri) uygulamadan bağımsız dosyalara çevirir:

- `<not>.pdf` — slaytlar özgün (vektör) halleriyle, üzerine el yazısı, fosforlu kalem, resimler ve metin kutuları çizilmiş
- `<not>.md` — sayfa sayfa slayt metni, metin kutuları, Notability'nin kendi el yazısı tanıması
- `<not> - kayıt N.m4a` — ses kayıtları (olduğu gibi kopyalanır)

Klasör düzeni Notability'deki kategori ağacıdır. Kaynak notlar yalnız okunur; hiçbir not silinmez ya da değiştirilmez.

## Komutlar

```bash
cd notability-export
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

# Tam çalıştırma. Kesilirse AYNI KOMUT yeniden çalıştırılır, kaldığı yerden devam eder.
nohup caffeinate -i .venv/bin/python batch.py /Volumes/NAS/Notability --jobs 6 >> durum/run.log 2>&1 &

# İlerleme
tail -f durum/run.log

# Doğrulama: plan <-> rapor <-> hedefteki dosyalar, her PDF açılır
.venv/bin/python batch.py /Volumes/NAS/Notability --verify

# 3 kez başarısız olmuş notları yeniden dene
.venv/bin/python batch.py /Volumes/NAS/Notability --retry-failed

# Tek not
.venv/bin/python nbconv.py "<...>/Documents/Not.nbn" /hedef/klasor "Kategori"
```

Çıkış kodları: 0 tamam, 1 bitti ama hatalı not var, 2 hedef birim bağlı değil, 3 zaten çalışıyor,
4 güvenlik kuralı durdurdu (disk / art arda 20 hata), 5 sinyalle durduruldu.

## Devam etme nasıl çalışır

| Dosya | İçerik |
|---|---|
| `durum/plan.tsv` | not → (kategori klasörü, dosya adı). Bir kez üretilir, adlar çalıştırmalar arasında değişmez. |
| `durum/rapor.tsv` | Her deneme için bir satır; yalnız eklenir, her satırdan sonra diske yazdırılır. |
| `durum/run.log` | Zaman damgalı günlük. |
| `<hedef>/_rapor/` | plan ve raporun her temiz çıkışta alınan kopyası. |

- Bir not yalnızca son satırı `ok` ise **ve** hedefte `.pdf` + `.md` rapordaki boyutla duruyorsa atlanır.
- Dosyalar hedefe `.partial-<pid>-<ad>` adıyla yazılır, boyutu doğrulanır, sonra yeniden adlandırılır.
  `.md` en son yazılır. Yarım kalan `.partial-*` dosyaları bir sonraki başlangıçta silinir.
- Hedef birim (örnekte `/Volumes/NAS`) bağlı değilse hiçbir şey yazılmaz; 10 dk yeniden bağlanma denenir.
- Dönüştürülen her not `brctl evict` ile iCloud'a geri bırakılır; Mac'te yer tutmaz.
- Durum dosyaları Mac'te durur: paylaşım düşse de yazılabilir.

## Biçim notları (notların kendisinden ölçüldü)

- `Session.plist` bir NSKeyedArchiver; kök `$0` = `NoteTakingSession`.
- Not, genişliği `reflowState.pageWidthInDocumentCoordsKey` (W) olan tek bir dikey tuvaldir; sayfalar aralıksız dizilir.
- **PDF'li sayfanın yüksekliği: `s = W / w`, `H = ceil(y1·s) − floor(y0·s)`, tek duyarlıkla (float32).**
  Yani ölçeklenmiş görünür kutu (CropBox, yoksa MediaBox; PDF'in kendi koordinatlarıyla) tam sayıya genişletilir.
  Kutu 0'dan başlıyorsa bu `ceil(h·s)` olur. 125 not / 720 indeksli sayfada tam değerle uyum %29,0, yukarı
  yuvarlamayla %97,1 (tam değer sayfa başına 0,4–0,8 birim kayar).
  Kutunun başlangıcı önemlidir: aynı boyuttaki (611,859 × 792) iki kırpma kutusu W 610'da, y 10,729'dan başlayınca
  791, y 13,271'den başlayınca 790 verir. 234 sayfalık bir notta ölçülen kaymanın her adımı (9., 74., 81. ve 136.
  sayfalarda 1, 2, 3 ve 5 birim) ilk türden bir sayfaya denk gelir; kural ikinci bir notta da doğrulandı (0/3 → 3/3).
  Tek duyarlık şart: Notability'nin kendi ürettiği PDF'lerde çarpım tam sayıya denk gelir. Çift duyarlıkta hem
  593,33 × 842,7541 / 595,2756 hem 716,8 × 594,442 / 792 tam sayının ~3,5e-5 üstünde çıkar; oysa ilki 840 kalmalı,
  ikincisi 539'a yuvarlanmalı. Bunları hiçbir tolerans ayıramaz (denendi: 1e-6 birini, 1e-3 diğerini bozuyor);
  float32 sırasıyla 840,0 ve 538,00006 verir ve 6 örneğin 6'sında doğrudur.
- Birkaç notta kayıtlı tuval genişliği mürekkebin yerleştirildiği genişlik değildir (görülen: kâğıdı sonradan A3'e
  çevrilmiş not, kayıtlı 839,1 / gerçek 716,8). İndeks bütün sayfaları reddederse bilinen diğer genişlikler denenir.
- **Kâğıt (PDF'siz) sayfa yüksekliği `W · 21/16`**; "letter" için 11/8,5 değil. A4 kâğıt yerel notlarda ölçülemedi,
  varsayılan 297/210. Her not kendi el yazısı indeksiyle yeniden sınanır (`oran_kaynagi` sütunu).
- Sayfa yerleşimi `NBNoteTakingSessionDocumentPaperLayoutModelKey.pageLayoutArray` içinde; biçim 5 notlarda bu
  anahtar yoktur, yerleşim `richText.pageLayoutArray` altındadır (`kPageLayoutPDFFileKey` bir PDFFile nesnesi).
- Eski `paperIndex` N = `paperIdentifier` `Legacy:N+1` (13 beyaz, 15 siyah).
- `pdfFiles` içindeki `type: 1` kayıtları belge değil **kâğıt şablonudur**. Yalnızca kâğıt kimliği onu gösteriyorsa
  (`TemplatePDF:<uuid>`) sayfa arka planı olarak çizilir; sayfa yüksekliği yine kâğıt oranından gelir. Kullanılmayan
  eski bir şablon çizilmez (görülen: siyah kâğıtlı notta duran beyaz çizgili şablon; çizilince hem arka plan yanlış
  oluyor hem ikinci sayfa 12 birim kayıyordu).
- **Karakter düzeyinde hiza ölçümü** (`hiza_olc.py`): indeksteki `characterRects`, karakter başına dört yarı
  duyarlıklı sayıdır (x, y, genişlik, yükseklik; `pageContentOrigin`'e göre) ve karakterin çizgilerini ~5 birim payla
  sarar. Mürekkep dikey kaydırılıp kutusuna tam oturan karakterler sayılır; en çok eşleşmeyi veren kayma gerçek
  konumdur. Çizimlerden, fosforlu kalemden ve sonradan eklenen yazıdan etkilenmez. Doğru hizalı notlarda sonuç
  −1,0 ile +1,0 birim arasındadır (81 sayfada ölçüldü).
- Mürekkep `richText['Handwriting Overlay'].SpatialHash` içinde: `curvespoints` (float32 x,y), `curvesnumpoints`,
  her eğri 3k+1 noktalı kübik bezier zinciri, `curvesfractionalwidths`, `curveswidth`, `curvescolors` (RGBA),
  `curvesstyles` (3 kalem, 4 fosforlu).
- `HandwritingIndex/index.plist` → `pageContentOrigin`: o sayfadaki ilk çizginin (kalınlık dahil) sayfa içi konumu.
  Fosforlu kalemin indekse girip girmediği nota göre değişir (2021 tarihli bir notta giriyor, 2025 tarihli bir notta
  girmiyor); bu yüzden sayfa, ilk kalem çizgisi **ya da** türü ne olursa olsun ilk çizgi uyuyorsa uyumlu sayılır.
  Geometri doğrulaması bununla yapılır (`indeks_sayfa`, `indeks_uyan` sütunları); kabul payı 1,5 birim + çizgi
  kalınlığının 0,15 katıdır (kalın kalemde kutu çizginin dış hattını izler).
- Kategori ağacı ve not→konu eşlemesi Mac'teki Notability uygulamasından okunur:
  `~/Library/Containers/com.gingerlabs.Notability/Data/Library/Organizer/*/assets/1/Asset` ve `NBMetadataCache.plist`.

## Dikkat

- **iCloud paketleri bütün olarak iner.** Buluttaki bir `.nbn` paketinin içini listelemek (`ls -R`, `du`, `find`)
  paketin tamamını indirir. Kapsayıcıda özyinelemeli komut çalıştırma. Yalnız paketin kendisine `stat` güvenlidir.
- `brctl evict` mutlak yol ister; adında `:` olan göreli yol "izin yok" hatası verir.
- **NAS'taki PDF'leri SMB üzerinden toplu tarama.** Tek süreçte çok iş parçacığıyla PyMuPDF kullanmak süreci
  çökertti (SIGBUS) ve aynı anda bütün SMB paylaşımları koptu. SMB üzerinden tek tek açmak da yavaş (4350 PDF ≈ 18 dk).
  Sayfa boyutları gerekiyorsa taramayı NAS'ın kendisinde, ssh üzerinden çalıştır (yalnız standart Python ile
  yazılmış salt okunur bir betikle 55 GB ≈ 2 dk 20 sn sürdü).
- Yalnız belirli notları yeniden çevirmek için: `batch.py <hedef> --names liste.txt --force`.
- Dosya adlarında `\ / : * ? " < > |` karakterleri `-` olur, sondaki nokta/boşluk atılır, adlar NFC'dir.
  Özgün ad `.md` başlığında ve `rapor.tsv`'de durur.

## 2026-09-27 çalıştırması

| | |
|---|---|
| Not | 4366: 4350 dönüştü, 16 boş paket, 0 hata |
| Çıktı | 4350 PDF + 4350 Markdown + 594 ses; 662 klasör; 55,17 GB |
| İçerik | 254.103 sayfa; 1099 notta 481.211 çizgi; 1288 resim; 367 metin kutusu |
| Süre | 110,3 dk + 14,5 dk (bir kez güvenlik kuralıyla durdu, aynı komutla devam etti) + düzeltme turları |
| Dosya doğrulaması | `--verify`: 4350 tamam, yarım dosya 0. Pi üzerinde bağımsız tarama: 4350 PDF'in sayfa sayısı ve boyutu raporla aynı |
| Geometri doğrulaması | Üzerinde el yazısı/resim/metin kutusu olan 1166 notun hepsi tarandı: 928 indeksli not, 6470 sayfa, 6386 uyumlu (%98,70); 861 not tam uyumlu |

- İçeriği iCloud'da eksik: 16 boş paket ve slayt PDF'i pakette olmayan 9 not (325 sayfa).
- Düzeltme turları: float32 kuralı için 67 not (7'sinin çıktısı değişti), kırpma kutusu kuralı için 1 + 4 not,
  kâğıt şablonu kuralı için 8 not (1'inde arka plan ve hiza yanlıştı, 7'sinde fark 0,4 birimdi).
- Tek sayfası uymayan 63 not karakter düzeyinde ölçüldü:
  42'sinde sayfanın kendisi doğrulandı (kayma ≤ 1 birim); 11'inde sayfada kutusuna tam oturan karakter var ama
  sayı az (zayıf kanıt); 6'sında sonraki sayfalar hizalı (dolaylı); 3'ünde sayfada çizgi yok ya da tek çizgi var,
  yalnız önceki sayfalar ölçülebildi; 1'inde hata bulundu ve düzeltildi (kâğıt şablonu).
- İkiden fazla sayfası uymayan üç not sayfa sayfa incelendi; üçünde de çıktı doğru, uyumsuzluk indeksin
  kapsamından geliyor:
  - Birinci not (66/71) ve ikinci not (4/6): uymayan
    sayfalarda indeks başlangıcının üstünde daire, alt çizgi gibi çizimler ya da indekse girmemiş yazı var.
    Çizimler slayttaki kelimelerin üzerinde (görsel olarak doğrulandı).
  - Üçüncü not (15/18): ilk 15 sayfa tam uyumlu (fark 0,0). İndeksin 17–19. sayfa kayıtları notun şimdiki
    verisindeki hiçbir çizgiye denk gelmiyor (o aralıkta çizgi yok); bu kayıtlar eskimiş.
  - Yani indeks uyumsuzluğu her zaman hiza hatası değildir: indeks çizimleri ve sonradan eklenen yazıyı kapsamaz.
    Birikimli kayma (komşu sayfalarda büyüyen fark) hata işaretidir; tekil sapma değildir.
- Slayt PDF'i eksik olan bir not (0/11) doğrulanamaz.
- Not başına sonuç tabloları (`durum/` altında) not başlıkları içerdiği için bu depoda yok.
- Kategori ağacı Mac'teki Notability uygulamasının 27–29 Ocak 2026 eşitlemesine aittir.
- Ağaçta olup iCloud'da paketi olmayan 240 not kimliği vardı.

## Sınırlar

- Notability'nin el yazısı tanıması gürültülüdür; Markdown'da ayrı ve etiketli durur.
- Nota yazılmış düz metin gövdesi yalnız Markdown'a girer, PDF'e çizilmez.
- Kâğıt desenleri (çizgi/nokta) yaklaşık çizilir. Bilinmeyen kâğıt rengi beyaz çizilir ve rapora uyarı düşer.
- Kaynak PDF'teki açıklama (annotation) nesneleri sayfa içeriğinin parçası değilse aktarılmaz.
- Sonradan değişen notlar yeniden dönüştürülmez (artımlı eşitleme yok).
- `brctl evict` macOS 27.2.1'de `brctl` yardım çıktısında listelenmiyor; bu sürümde çalışıp çalışmadığı sınanmadı.
  Çalışmazsa dönüştürülen notlar Mac'te kalır ve çalıştırma disk güvenlik kuralıyla durur (çıkış kodu 4);
  `--no-evict` bu adımı kapatır.
- Bu depoda otomatik test yok; sonuçlar yukarıdaki 2026-09-27 çalıştırmasının ölçümleridir.

## Lisans

[MIT](LICENSE).
