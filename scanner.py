"""
scanner.py — GitHub Actions'ta çalışan KAP tarayıcı
KAP API engellendiği için HTML sayfası scrape edilir.
"""

import os
import json
import logging
import requests
from datetime import datetime
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

KATALIZ_ESIK  = 70
HEDEF_GETIRI  = 35.0
STOP_LOSS     = 10.0
MAX_SURE_SAAT = 48

POZITIF = [
    "sözleşme", "sozlesme", "anlaşma", "anlasma", "mukavele",
    "sipariş", "siparis", "ihracat", "ihraç", "tedarik",
    "ortaklık", "ortaklik", "işbirliği", "isbirligi",
    "joint venture", "konsorsiyum", "ihale kazanıldı",
    "satış sözleşmesi", "lisans anlaşması", "yurt dışı sipariş",
]
NEGATIF = [
    "faaliyet raporu", "bilanço", "bilanco", "genel kurul",
    "temettü", "temettu", "bağımsız denetim", "yönetim kurulu üye",
    "sermaye artırımı", "hisse geri alım",
]

# ─────────────────────────────────────────────────────────────────────────────
#  Yardımcı
# ─────────────────────────────────────────────────────────────────────────────

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
#  KAP HTML Scraper
# ─────────────────────────────────────────────────────────────────────────────

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "tr-TR,tr;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}

def kap_ac() -> list:
    """KAP bildirimler sayfasını HTML olarak okur."""
    url = "https://www.kap.org.tr/tr/Bildirimler/Genel"
    haberler = []

    try:
        session = requests.Session()
        # Önce ana sayfayı ziyaret et (cookie al)
        session.get("https://www.kap.org.tr", headers=HEADERS, timeout=15)

        r = session.get(url, headers=HEADERS, timeout=20)
        log.info(f"KAP HTML status: {r.status_code}")
        log.info(f"İçerik uzunluğu: {len(r.text)} karakter")

        soup = BeautifulSoup(r.text, "html.parser")

        # KAP bildirim satırlarını bul
        # Farklı CSS sınıflarını dene
        satirlar = (
            soup.find_all("tr", class_=lambda c: c and "disclosure" in str(c).lower())
            or soup.find_all("div", class_=lambda c: c and "disclosure" in str(c).lower())
            or soup.select("table.w-100 tr")
            or soup.select(".disclosureList tr")
            or soup.find_all("tr")[1:]  # Tablo varsa başlığı atla
        )

        log.info(f"Bulunan satır sayısı: {len(satirlar)}")

        for satir in satirlar[:50]:
            hucre = satir.find_all("td")
            if len(hucre) < 3:
                continue

            metinler = [h.get_text(strip=True) for h in hucre]
            log.info(f"Satır: {metinler[:4]}")

            # KAP tablo yapısı genellikle: tarih | şirket | başlık | ...
            haber = {
                "id": "",
                "zaman": metinler[0] if len(metinler) > 0 else "",
                "sirket": metinler[1] if len(metinler) > 1 else "",
                "kod": "",
                "baslik": metinler[2] if len(metinler) > 2 else "",
            }

            # Link varsa ID'yi al
            link = satir.find("a", href=True)
            if link:
                href = link.get("href", "")
                # /tr/Bildirim/12345 formatından ID çek
                parcalar = href.rstrip("/").split("/")
                if parcalar:
                    haber["id"] = parcalar[-1]
                if not haber["baslik"]:
                    haber["baslik"] = link.get_text(strip=True)

            if haber["baslik"] and len(haber["baslik"]) > 5:
                haberler.append(haber)

    except Exception as e:
        log.error(f"KAP scrape hatası: {e}")

    log.info(f"Toplam çekilen haber: {len(haberler)}")
    return haberler

# ─────────────────────────────────────────────────────────────────────────────
#  Filtre
# ─────────────────────────────────────────────────────────────────────────────

def on_filtre(baslik: str) -> bool:
    metin = baslik.lower()
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

GPT_SISTEM = """Sen bir BIST uzman analistisin. KAP açıklamalarını analiz ederek
kısa vadeli fiyat katalizi yaratabilecek haberleri 0-100 arasında skorluyorsun.
SADECE JSON yanıt ver, başka metin ekleme."""

GPT_PROMPT = """Şirket: {sirket} ({kod})
Başlık: {baslik}

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

def gpt_analiz(sirket, kod, baslik) -> dict:
    if not OPENAI_AVAILABLE or not OPENAI_API_KEY:
        return kural_analiz(baslik)
    try:
        client = OpenAI(api_key=OPENAI_API_KEY)
        resp = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": GPT_SISTEM},
                {"role": "user", "content": GPT_PROMPT.format(
                    sirket=sirket, kod=kod, baslik=baslik
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
        return kural_analiz(baslik)

def kural_analiz(baslik: str) -> dict:
    metin = baslik.lower()
    skor = 50
    artiranlar = {
        "milyon": 15, "milyar": 25, "usd": 10, "eur": 10,
        "yurt dışı": 10, "uluslararası": 8, "uzun vadeli": 8,
        "çok yıllık": 10, "münhasır": 12
    }
    dusururenler = {
        "ön anlaşma": -15, "niyet mektubu": -12,
        "mou": -12, "görüşme": -10, "değerlendirilmekte": -8
    }
    for k, v in artiranlar.items():
        if k in metin:
            skor += v
    for k, v in dusururenler.items():
        if k in metin:
            skor += v
    return {
        "kataliz_skoru": max(0, min(100, skor)),
        "ozet": baslik[:120],
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
    skor_emoji = "🔴" if h["skor"] >= 85 else "🟡" if h["skor"] >= 70 else "⚪"
    return (
        f"{skor_emoji} *KATALİZ SİNYALİ*\n\n"
        f"🏢 *{h['sirket']}* (`{h['kod']}`)\n"
        f"📰 {h['baslik']}\n\n"
        f"🎯 Skor: *{h['skor']}/100*\n"
        f"💡 {h['ozet']}\n"
        f"📊 Büyüklük: {h['anlasma_buyuklugu']}\n"
        f"🤝 Karşı taraf: {h['karsi_taraf']}\n"
        f"🔄 Tekrarlayan: {h['tekrarlayan']}\n\n"
        f"⚡ Hedef: +{HEDEF_GETIRI}%  |  Stop: -{STOP_LOSS}%  |  Max {MAX_SURE_SAAT}sa\n"
        f"🕐 {h['zaman']}\n"
        f"🔗 https://www.kap.org.tr/tr/Bildirim/{h['id']}"
    )

# ─────────────────────────────────────────────────────────────────────────────
#  Ana Döngü
# ─────────────────────────────────────────────────────────────────────────────

def main():
    log.info("═══ KAP Taraması Başladı ═══")

    gorulmus   = set(yukle_json(SEEN_FILE, []))
    sinyaller  = yukle_json(SIGNALS_FILE, [])
    tarama_log = yukle_json(SCANLOG_FILE, [])

    haberler = kap_ac()
    log.info(f"{len(haberler)} açıklama işlenecek")

    yeni_sinyal = 0

    for h in haberler:
        hid    = str(h.get("id", ""))
        baslik = h.get("baslik", "")
        sirket = h.get("sirket", "")
        kod    = h.get("kod", "")
        zaman  = h.get("zaman", "")

        if not baslik:
            continue

        # ID yoksa başlıktan üret
        if not hid:
            import hashlib
            hid = hashlib.md5(baslik.encode()).hexdigest()[:12]

        if hid in gorulmus:
            continue

        gorulmus.add(hid)

        if not on_filtre(baslik):
            continue

        log.info(f"Aday: [{kod}] {baslik[:60]}")
        analiz = gpt_analiz(sirket, kod, baslik)
        skor   = analiz.get("kataliz_skoru", 0)
        log.info(f"  → Skor: {skor}")

        if skor >= KATALIZ_ESIK:
            sinyal = {
                "id":                hid,
                "zaman":             zaman,
                "tarama_zamani":     datetime.utcnow().isoformat() + "Z",
                "sirket":            sirket,
                "kod":               kod,
                "baslik":            baslik,
                "skor":              skor,
                "ozet":              analiz.get("ozet", ""),
                "anlasma_buyuklugu": analiz.get("anlasma_buyuklugu", ""),
                "tekrarlayan":       analiz.get("tekrarlayan", ""),
                "karsi_taraf":       analiz.get("karsi_taraf", ""),
                "kataliz_tipi":      analiz.get("kataliz_tipi", ""),
                "risk":              analiz.get("risk", ""),
                "url":               f"https://www.kap.org.tr/tr/Bildirim/{hid}",
            }
            sinyaller.insert(0, sinyal)
            telegram_gonder(sinyal_mesaji(sinyal))
            yeni_sinyal += 1
            log.info(f"  ✅ SİNYAL: [{kod}] Skor={skor}")

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