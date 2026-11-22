# 00 · Analistin gözü (kodsuz; Antalya akşamları için)

Hedef: bir aracı kurum raporunu okuyup "bu rakam neden önemli?" sorusunu cevaplayabilmek ve kendi
3 hissen için bir sayfalık tez yazmak. CFA Level I'deki *Financial Statement Analysis* ve *Equity*
konularının neredeyse aynısı; buradaki her şey sınavda da işine yarar.

## 1. Her analistin baktığı 7 şey

| # | Soru | Bakılan rakamlar | Tuzak |
|---|---|---|---|
| 1 | Şirket parayı nereden kazanıyor? | Segment gelirleri, coğrafi dağılım, müşteri/ürün yoğunlaşması | "Ne yaptığını" bilmeden çarpana bakmak |
| 2 | Büyüyor mu? | Hasılat büyümesi **reel** (enflasyondan arındırılmış) ve **$ bazında** | TL'de %40 büyüme, %35 enflasyonda neredeyse sıfırdır |
| 3 | Kârlı mı, kârlılık nereye gidiyor? | Brüt marj, FAVÖK marjı, net marj, ROE, ROIC | Tek seferlik gelirler (arsa satışı, kur farkı) net kârı şişirir |
| 4 | Bilanço sağlam mı? | Net borç, Net borç / FAVÖK, **döviz pozisyonu**, vade yapısı | TL değer kaybında açık döviz pozisyonu kârı siler |
| 5 | Nakit üretiyor mu? | Faaliyetlerden nakit, yatırım harcaması (capex), serbest nakit akışı (FCF) | Kâr var ama nakit yok → alacak/stok şişiyor |
| 6 | Ucuz mu? | F/K, FD/FAVÖK, PD/DD, temettü verimi; **benzer şirketlere** ve **kendi geçmişine** göre; DCF | Düşük F/K bazen "kâr düşecek" demektir (değer tuzağı) |
| 7 | Ne değişirse fiyat oynar? | Katalizörler (bilanço tarihi, yeni sözleşme, regülasyon), riskler, konsensüs beklentisi | Fiyat haberi değil, **beklentiden sapmayı** fiyatlar |

**Türkiye'ye özel:** 2023 sonundan beri birçok şirket enflasyon muhasebesi (TMS 29) uyguluyor;
karşılaştırma dönemleri bugünkü paraya çevrilir. Raporda "enflasyon düzeltilmiş" ifadesini ara ve
farklı şirketleri karşılaştırırken aynı bazda olduklarından emin ol.

## 2. Sektöre özel göstergeler (senin hisselerin)

**THYAO (havayolu)**
- ASK (sunulan koltuk-km = kapasite), RPK (yolcu-km), **doluluk oranı** = RPK / ASK
- **Yield** (yolcu-km başına gelir), **RASK** (ASK başına gelir), **CASK** (ASK başına maliyet), **CASK hariç yakıt**
- Yakıtın maliyetteki payı, **yakıt hedge oranı**, kargo gelirinin payı, transfer yolcu oranı
- Neden: havayolunda kâr = (RASK − CASK) × ASK. Petrol 10 $ artınca CASK ne kadar artar, bilet fiyatı bunu karşılar mı?

**TUPRS (rafineri)**
- **Rafineri marjı / crack spread** ($/varil: ürün fiyatları − ham petrol), kapasite kullanım oranı
- Ürün dağılımı (dizel, jet yakıtı, benzin), **stok kazancı/zararı** (petrol fiyatı düşünce stok değer kaybeder)
- Neden: Tüpraş'ın kârı petrolün seviyesinden çok **marja** bağlı.

**CCOLA (şişeleme)**
- **Hacim** (ünite kasa), fiyat/ürün karması (price/mix), ülke dağılımı (Türkiye, Pakistan, Kazakistan, Özbekistan…)
- Şeker, PET, alüminyum maliyetleri; yerel para birimlerinin $'a karşı seyri
- Neden: gelir çok ülkeli ve kur hareketleri raporlanan TL rakamları çok oynatır.

**TI1 (para piyasası fonu)**
- Getirisi **TCMB politika faizine** yakın olmalı; fark = gider oranı + portföy (repo, mevduat, kısa vadeli bono)
- Stopaj, yıllık getiri vs enflasyon (reel getiri), fon büyüklüğü
- Neden: bu fon portföyünün "nakit" ayağı; hisseleri bununla karşılaştır (hisse, fonun getirisini geçiyor mu?).

## 3. Görevler

Kaynak: KAP'ta şirketin "Finansal Rapor" bildirimleri (papi: `papi kap show <indeks>`), şirketin yatırımcı
ilişkileri sunumu, aracı kurum raporları (Ziraat, İş Yatırım, Garanti BBVA Yatırım ücretsiz yayınlıyor).

1. **THYAO tablosu.** Son 4 çeyrek için aşağıdaki tabloyu elle doldur:

   | Çeyrek | Hasılat | FAVÖK | FAVÖK marjı | Net kâr | Net borç | Doluluk | Yakıt / toplam maliyet |
   |---|---|---|---|---|---|---|---|
   | 3Ç25 | | | | | | | |
   | 4Ç25 | | | | | | | |
   | 1Ç26 | | | | | | | |
   | 2Ç26 | 327,1 mlr TL | 22,7 mlr TL | %6,9 | 8,95 mlr TL | | | |

   Soru: 2Ç26'da marj neden çöktü? Bunu üç rakamla açıkla.

2. **Çarpanları kendin hesapla.** Piyasa değeri = fiyat × pay adedi (THYAO: 1,38 milyar pay). Firma değeri
   = piyasa değeri + net borç. Son 4 çeyreğin toplam net kârı ve FAVÖK'ü ile F/K ve FD/FAVÖK hesapla;
   Ziraat raporundaki 3,1x ve 6,6x ile karşılaştır. Fark varsa nedenini bul.

3. **3Ç26 bilançosu gelince** (Kasım başı): İş Yatırım'ın beklentisi FAVÖK marjı %22,3, net kâr 1,1 mlr $.
   Gerçekleşen ile karşılaştır, hissenin ertesi günkü tepkisini papi'de gör (`papi prices show THYAO`).
   Beklentiyi aştı mı, fiyat ne yaptı? Bu, 03. notebook'taki olay çalışmasının elle yapılmışı.

4. **Bir sayfalık tez** (her hisse için): "Bu hisseyi tutuyorum çünkü … . Fikrimi şu değiştirir: … .
   İzlediğim 3 rakam: … ." Bunu `learn/tezler.md` olarak kaydet; 3 ay sonra tekrar oku.

5. **TI1 vs hisseler.** TI1'in son 1 yıllık getirisini politika faiziyle karşılaştır. Aynı dönemde
   THYAO/TUPRS/CCOLA TI1'i geçti mi? (01. notebook bunu kodla yapacak; önce tahminini yaz.)
