"""
scanner.py — GitHub Actions'ta çalışan KAP tarayıcı
Kaynak: finans.mynet.com/borsa/kaphaberleri/
Hisse kodu (***KDMR***) ile birlikte KAP bildirimlerini çeker.
48 saatten eski haberler otomatik elenir.
"""

import os
import json
import hashlib
import logging
import requests
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from bs4 import BeautifulSoup

try:
    from openai import OpenAI
    OPENAI_AVAILABLE = True
except ImportError:
    OPENAI_AVAILABLE = False

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

OPENAI_API_KEY   = os.environ.get("OPENAI_API_KEY", "")
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

SIGNALS_FILE = "data/signals.json"
SCANLOG_FILE = "data/scan_log.json"
SEEN_FILE    = "data/seen_ids.json"

KATALIZ_ESIK       = 70
HEDEF_GETIRI       = 35.0
STOP_LOSS          = 10.0
MAX_SURE_SAAT      = 48
MAX_HABER_YAS_SAAT = 48

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "tr-TR,tr;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

POZITIF = [
    "sözleşme", "sozlesme", "anlaşma", "anlasma", "mukavele",
    "sipariş", "siparis", "ihracat", "ihraç", "tedarik",
    "ortaklık", "ortaklik", "işbirliği", "isbirligi",
    "joint venture", "konsorsiyum", "ihale kazandı", "ihaleyi kazandı",
    "satış sözleşmesi", "lisans anlaşması", "yurt dışı sipariş",
    "özel durum açıklaması", "özel durum (genel)",
    "önemli sözleşme", "önemli anlaşma",
]
NEGATIF = [
    "faaliyet raporu", "bilanço", "bilanco", "genel kurul",
    "temettü", "temettu", "bağımsız denetim",
    "sermaye artırımı", "hisse geri alım", "finansal rapor",
    "sorumluluk beyanı", "kurumsal yönetim", "sürdürülebilirlik",
    "piyasa yapıcılığı", "katılım finansı bilgi formu",
]

def yukle_json(yol, varsayilan):
    try:
        with open(yol, "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return varsayilan

def kaydet_json(yol, veri):
    os.makedirs(os.path.dirname(yol), exist_ok=True)
    with open(yol, "w", encoding="utf-8") as f:
        json.dump(veri, f, ensure_ascii=False, indent=2)

# ─────────────────────────────────────────────────────────────────────────────
#  Tarih Kontrolü
# ─────────────────────────────────────────────────────────────────────────────

AYLAR = {
    "Oca": 1, "Şub": 2, "Mar": 3, "Nis": 4, "May": 5, "Haz": 6,
    "Tem": 7, "Ağu": 8, "Eyl": 9, "Eki": 10, "Kas": 11, "Ara": 12,
    "Jan": 1, "Feb": 2, "Apr": 4, "Jun": 6, "Jul": 7, "Aug": 8,
    "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
}

def mynet_tarih_parse(tarih_str: str):
    """'14 Şub 2026 09:37' → datetime"""
    try:
        parcalar = tarih_str.strip().split()
        if len(parcalar) == 4:
            gun, ay_str, yil, saat = parcalar
            ay = AYLAR.get(ay_str[:3], 0)
            if ay == 0:
                return None
            saat_p = saat.split(":")
            return datetime(int(yil), ay, int(gun),
                          int(saat_p[0]), int(saat_p[1]),
                          tzinfo=timezone.utc)
    except:
        return None

def haber_taze_mi(tarih_str: str) -> bool:
    """48 saatten yeni mi?"""
    if not tarih_str:
        return True
    dt = mynet_tarih_parse(tarih_str)
    if dt is None:
        return True
    fark_saat = (datetime.now(timezone.utc) - dt).total_seconds() / 3600
    if fark_saat > MAX_HABER_YAS_SAAT:
        log.info(f"  ⏭ Eski haber atlandı ({int(fark_saat)} saat önce)")
        return False
    return True

# ─────────────────────────────────────────────────────────────────────────────
#  Mynet KAP Haberleri
# ─────────────────────────────────────────────────────────────────────────────

def mynet_kap_cek() -> list:
    url = "https://finans.mynet.com/borsa/kaphaberleri/"
    haberler = []
    try:
        r = requests.get(url, headers=HEADERS, timeout=20)
        log.info(f"Mynet KAP status: {r.status_code}, uzunluk: {len(r.text)}")

        soup = BeautifulSoup(r.text, "html.parser")

        # Tüm haber linklerini bul
        # Format: ***HISSE*** ŞİRKET ADI (bildirim tipi) → link text ve href
        linkler = soup.find_all("a", href=re.compile(r"/borsa/haberdetay/"))

        log.info(f"Bulunan KAP haberi linki: {len(linkler)}")

        for link in linkler:
            metin = link.get_text(strip=True)
            if not metin or "***" not in metin:
                continue

            href = link.get("href", "")
            tam_url = f"https://finans.mynet.com{href}" if href.startswith("/") else href

            # Hisse kodu çıkar: ***KDMR***
            kod_match = re.search(r"\*\*\*([A-Z0-9]+)\*\*\*", metin)
            hisse_kodu = kod_match.group(1) if kod_match else ""

            # Bildirim tipi çıkar: (Özel Durum Açıklaması)
            tip_match = re.search(r"\(([^)]+)\)\s*$", metin)
            bildirim_tipi = tip_match.group(1) if tip_match else ""

            # Şirket adını çıkar: ***KDMR*** KARDEMİR... (bildirim) → şirket adı
            sirket_adi = re.sub(r"\*\*\*[A-Z0-9]+\*\*\*\s*", "", metin)
            sirket_adi = re.sub(r"\s*\([^)]+\)\s*$", "", sirket_adi).strip()

            # Tarih: genellikle li veya parent elementinde
            tarih_str = ""
            parent = link.parent
            if parent:
                tarih_match = re.search(
                    r"\d{1,2}\s+(?:Oca|Şub|Mar|Nis|May|Haz|Tem|Ağu|Eyl|Eki|Kas|Ara)\s+\d{4}\s+\d{2}:\d{2}",
                    parent.get_text()
                )
                if tarih_match:
                    tarih_str = tarih_match.group(0)

            hid = hashlib.md5(tam_url.encode("utf-8")).hexdigest()[:12]

            haberler.append({
                "id":              hid,
                "baslik":          metin.strip(),
                "sirket":          sirket_adi,
                "kod":             hisse_kodu,
                "bildirim_tipi":   bildirim_tipi,
                "url":             tam_url,
                "zaman":           tarih_str,
            })

    except Exception as e:
        log.error(f"Mynet KAP hatası: {e}")

    return haberler

# ─────────────────────────────────────────────────────────────────────────────
#  Filtre
# ─────────────────────────────────────────────────────────────────────────────

def on_filtre(baslik: str, zaman: str = "", bildirim_tipi: str = "") -> bool:
    # 1. Tarih filtresi
    if not haber_taze_mi(zaman):
        return False

    metin = baslik.lower()
    tip   = bildirim_tipi.lower()

    # 2. Negatif bildirim tipleri — raporlar, kurumsal vs. direkt elensin
    NEGATIF_TIPLER = [
        "finansal rapor", "faaliyet raporu", "sorumluluk beyanı",
        "kurumsal yönetim", "sürdürülebilirlik", "katılım finansı",
        "bağımsız denetim", "piyasa yapıcılığı", "sermaye artırımı",
        "temettü", "genel kurul",
    ]
    for t in NEGATIF_TIPLER:
        if t in tip:
            return False

    # 3. Güçlü pozitif tipler — doğrudan geçir
    POZITIF_TIPLER = [
        "özel durum", "önemli sözleşme", "önemli anlaşma",
    ]
    for t in POZITIF_TIPLER:
        if t in tip:
            return True

    # 4. Metin tabanlı filtre
    for k in NEGATIF:
        if k in metin:
            return False
    for k in POZITIF:
        if k in metin:
            return True

    return False

# ─────────────────────────────────────────────────────────────────────────────
#  Analiz
# ─────────────────────────────────────────────────────────────────────────────

GPT_SISTEM = """Sen bir BIST uzman analistisin. KAP bildirimleri analiz ederek
kısa vadeli fiyat katalizi yaratabilecekleri 0-100 arasında skorluyorsun.
SADECE JSON yanıt ver, başka metin ekleme."""

GPT_PROMPT = """Hisse kodu: {kod}
Şirket: {sirket}
Bildirim başlığı: {baslik}
Bildirim tipi: {bildirim_tipi}

JSON formatı:
{{
  "kataliz_skoru": 0-100,
  "ozet": "1-2 cümle",
  "anlasma_buyuklugu": "rakam veya Belirtilmemiş",
  "tekrarlayan": "Evet/Hayır/Belirsiz",
  "karsi_taraf": "şirket/ülke adı veya Bilinmiyor",
  "kataliz_tipi": "Sözleşme/Sipariş/Ortaklık/İhracat/Diğer",
  "risk": "varsa belirt, yoksa Yok"
}}"""

def gpt_analiz(h: dict) -> dict:
    if not OPENAI_AVAILABLE or not OPENAI_API_KEY:
        return kural_analiz(h)
    try:
        client = OpenAI(api_key=OPENAI_API_KEY)
        resp = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": GPT_SISTEM},
                {"role": "user", "content": GPT_PROMPT.format(
                    kod=h.get("kod", ""),
                    sirket=h.get("sirket", ""),
                    baslik=h.get("baslik", ""),
                    bildirim_tipi=h.get("bildirim_tipi", ""),
                )},
            ],
            temperature=0.1,
            max_tokens=400,
        )
        raw = resp.choices[0].message.content.strip()
        raw = raw.replace("```json", "").replace("```", "").strip()
        return json.loads(raw)
    except Exception as e:
        log.error(f"GPT hatası: {e}")
        return kural_analiz(h)

def kural_analiz(h: dict) -> dict:
    metin = h.get("baslik", "").lower()
    tip   = h.get("bildirim_tipi", "").lower()
    skor  = 50

    # Bildirim tipine göre baz skor
    if "özel durum" in tip:
        skor += 10
    if "önemli" in tip:
        skor += 15

    for k, v in {
        "milyon": 15, "milyar": 25, "usd": 10, "eur": 10, "dolar": 8,
        "yurt dışı": 10, "uluslararası": 8, "uzun vadeli": 8,
        "çok yıllık": 10, "münhasır": 12, "ihale kazandı": 20,
        "büyük sipariş": 18, "dev anlaşma": 20, "ihracat": 8,
        "sipariş": 12, "sözleşme": 10, "anlaşma": 10,
    }.items():
        if k in metin:
            skor += v

    for k, v in {
        "ön anlaşma": -15, "niyet mektubu": -12,
        "mou": -12, "görüşme": -10, "değerlendirilmekte": -8,
    }.items():
        if k in metin:
            skor += v

    return {
        "kataliz_skoru": max(0, min(100, skor)),
        "ozet": h.get("baslik", "")[:120],
        "anlasma_buyuklugu": "Belirtilmemiş",
        "tekrarlayan": "Belirsiz",
        "karsi_taraf": "Bilinmiyor",
        "kataliz_tipi": "Diğer",
        "risk": "Yok",
    }

# ─────────────────────────────────────────────────────────────────────────────
#  Telegram
# ─────────────────────────────────────────────────────────────────────────────

def telegram_gonder(mesaj: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.info(f"[Telegram yok] {mesaj[:80]}")
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": mesaj,
                "parse_mode": "Markdown",
                "disable_web_page_preview": True,
            },
            timeout=10,
        )
        log.info("Telegram gönderildi.")
    except Exception as e:
        log.error(f"Telegram hatası: {e}")

def sinyal_mesaji(h: dict) -> str:
    emoji = "🔴" if h["skor"] >= 85 else "🟡" if h["skor"] >= 70 else "⚪"
    kod_str = f" `{h['kod']}`" if h.get("kod") else ""
    return (
        f"{emoji} *KATALİZ SİNYALİ*{kod_str}\n\n"
        f"🏢 {h.get('sirket', '')}\n"
        f"📋 {h.get('bildirim_tipi', '')}\n"
        f"📰 {h['baslik'][:200]}\n\n"
        f"🎯 Skor: *{h['skor']}/100*\n"
        f"💡 {h['ozet']}\n"
        f"📊 Büyüklük: {h['anlasma_buyuklugu']}\n"
        f"🤝 Karşı taraf: {h['karsi_taraf']}\n"
        f"🔄 Tekrarlayan: {h['tekrarlayan']}\n\n"
        f"⚡ Hedef: +{HEDEF_GETIRI}%  |  Stop: -{STOP_LOSS}%  |  Max {MAX_SURE_SAAT}sa\n"
        f"🕐 {h['zaman']}\n"
        f"🔗 {h['url']}"
    )

# ─────────────────────────────────────────────────────────────────────────────
#  Ana Döngü
# ─────────────────────────────────────────────────────────────────────────────

def main():
    log.info("═══ KAP Taraması Başladı ═══")

    gorulmus   = set(yukle_json(SEEN_FILE, []))
    sinyaller  = yukle_json(SIGNALS_FILE, [])
    tarama_log = yukle_json(SCANLOG_FILE, [])

    haberler    = mynet_kap_cek()
    yeni_sinyal = 0

    log.info(f"Toplam çekilen bildirim: {len(haberler)}")

    for h in haberler:
        hid    = h.get("id", "")
        baslik = h.get("baslik", "")

        if not baslik or hid in gorulmus:
            continue
        gorulmus.add(hid)

        if not on_filtre(baslik, h.get("zaman", ""), h.get("bildirim_tipi", "")):
            continue

        log.info(f"Aday: [{h.get('kod','')}] {baslik[:60]}")
        analiz = gpt_analiz(h)
        skor   = analiz.get("kataliz_skoru", 0)
        log.info(f"  → Skor: {skor}")

        if skor >= KATALIZ_ESIK:
            sinyal = {
                "id":              hid,
                "zaman":           h.get("zaman", ""),
                "tarama_zamani":   datetime.utcnow().isoformat() + "Z",
                "sirket":          h.get("sirket", ""),
                "kod":             h.get("kod", ""),
                "bildirim_tipi":   h.get("bildirim_tipi", ""),
                "baslik":          baslik,
                "skor":            skor,
                "ozet":            analiz.get("ozet", ""),
                "anlasma_buyuklugu": analiz.get("anlasma_buyuklugu", ""),
                "tekrarlayan":     analiz.get("tekrarlayan", ""),
                "karsi_taraf":     analiz.get("karsi_taraf", ""),
                "kataliz_tipi":    analiz.get("kataliz_tipi", ""),
                "risk":            analiz.get("risk", ""),
                "url":             h.get("url", ""),
            }
            sinyaller.insert(0, sinyal)
            telegram_gonder(sinyal_mesaji(sinyal))
            yeni_sinyal += 1
            log.info(f"  ✅ SİNYAL: [{h.get('kod','')}] Skor={skor}")

    sinyaller  = sinyaller[:200]
    tarama_log.insert(0, {
        "zaman":       datetime.utcnow().isoformat() + "Z",
        "taranan":     len(haberler),
        "yeni_sinyal": yeni_sinyal,
    })
    tarama_log = tarama_log[:100]

    kaydet_json(SIGNALS_FILE, sinyaller)
    kaydet_json(SCANLOG_FILE, tarama_log)
    kaydet_json(SEEN_FILE, list(gorulmus)[-2000:])
    log.info(f"═══ Tarama Bitti — {yeni_sinyal} yeni sinyal ═══")

if __name__ == "__main__":
    main()