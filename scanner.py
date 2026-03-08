"""
scanner.py — GitHub Actions'ta çalışan KAP tarayıcı

Çıktı: data/signals.json, data/scan_log.json
Bildirim: Telegram
"""

import os
import json
import hashlib
import logging
import requests
from datetime import datetime
from bs4 import BeautifulSoup

# ── OpenAI (opsiyonel) ────────────────────────────────────────────────────────
try:
    from openai import OpenAI
    OPENAI_AVAILABLE = True
except ImportError:
    OPENAI_AVAILABLE = False

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger(__name__)

# ── Ortam değişkenlerinden al ─────────────────────────────────────────────────
OPENAI_API_KEY   = os.environ.get("OPENAI_API_KEY", "")
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# ── Dosya yolları ─────────────────────────────────────────────────────────────
SIGNALS_FILE  = "data/signals.json"
SCANLOG_FILE  = "data/scan_log.json"
SEEN_FILE     = "data/seen_ids.json"

# ── Parametreler ──────────────────────────────────────────────────────────────
KATALIZ_ESIK     = 70    # 0-100 arası skor eşiği
HEDEF_GETIRI     = 35.0  # %
STOP_LOSS        = 10.0  # %
MAX_SURE_SAAT    = 48

# ── Anahtar kelimeler ─────────────────────────────────────────────────────────
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
#  Yardımcı Fonksiyonlar
# ─────────────────────────────────────────────────────────────────────────────

def yukle_json(yol: str, varsayilan):
    try:
        with open(yol, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return varsayilan

def kaydet_json(yol: str, veri):
    os.makedirs(os.path.dirname(yol), exist_ok=True)
    with open(yol, "w", encoding="utf-8") as f:
        json.dump(veri, f, ensure_ascii=False, indent=2)

# ─────────────────────────────────────────────────────────────────────────────
#  KAP Erişim
# ─────────────────────────────────────────────────────────────────────────────

def kap_ac(sayfa: int = 0) -> list:
    url = "https://www.kap.org.tr/tr/api/disclosures"
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; KAPRadar/1.0)",
        "Accept": "application/json",
        "Referer": "https://www.kap.org.tr/",
    }
    try:
        r = requests.get(url, params={"page": sayfa}, headers=headers, timeout=20)
        r.raise_for_status()
        data = r.json()
        # KAP bazen liste, bazen dict içinde liste döner
        if isinstance(data, list):
            return data
        return data.get("data", data.get("disclosures", []))
    except Exception as e:
        log.error(f"KAP erişim hatası: {e}")
        return []

def haber_detay_cek(disclosure_id: str) -> str:
    url = f"https://www.kap.org.tr/tr/Bildirim/{disclosure_id}"
    try:
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
        soup = BeautifulSoup(r.text, "html.parser")
        for div in soup.find_all("div", class_=lambda c: c and "disclosure" in c.lower()):
            text = div.get_text(" ", strip=True)
            if len(text) > 100:
                return text[:3000]
        return soup.get_text(" ", strip=True)[:2000]
    except Exception as e:
        log.error(f"Detay çekme hatası: {e}")
        return ""

# ─────────────────────────────────────────────────────────────────────────────
#  Filtreleme
# ─────────────────────────────────────────────────────────────────────────────

def on_filtre(baslik: str, ozet: str = "") -> bool:
    metin = (baslik + " " + ozet).lower()
    for k in NEGATIF:
        if k in metin:
            return False
    for k in POZITIF:
        if k in metin:
            return True
    return False

# ─────────────────────────────────────────────────────────────────────────────
#  Analiz — GPT-4o veya Kural
# ─────────────────────────────────────────────────────────────────────────────

GPT_SISTEM = """Sen bir BIST uzman analistisin. KAP açıklamalarını analiz ederek
kısa vadeli fiyat katalizi yaratabilecek haberleri 0-100 arasında skorluyorsun.
SADECE JSON yanıt ver, başka metin ekleme."""

GPT_PROMPT = """Şirket: {sirket} ({kod})
Başlık: {baslik}
İçerik: {icerik}

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

def gpt_analiz(sirket, kod, baslik, icerik) -> dict:
    if not OPENAI_AVAILABLE or not OPENAI_API_KEY:
        return kural_analiz(baslik, icerik)
    try:
        client = OpenAI(api_key=OPENAI_API_KEY)
        resp = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": GPT_SISTEM},
                {"role": "user", "content": GPT_PROMPT.format(
                    sirket=sirket, kod=kod, baslik=baslik, icerik=icerik[:2500]
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
        return kural_analiz(baslik, icerik)

def kural_analiz(baslik: str, icerik: str) -> dict:
    metin = (baslik + " " + icerik).lower()
    skor = 50
    artiranlar = {"milyon": 15, "milyar": 25, "usd": 10, "eur": 10,
                  "yurt dışı": 10, "uluslararası": 8, "uzun vadeli": 8,
                  "çok yıllık": 10, "münhasır": 12}
    dusururenler = {"ön anlaşma": -15, "niyet mektubu": -12,
                    "mou": -12, "görüşme": -10, "değerlendirilmekte": -8}
    for k, v in artiranlar.items():
        if k in metin:
            skor += v
    for k, v in dusururenler.items():
        if k in metin:
            skor += v
    skor = max(0, min(100, skor))
    return {
        "kataliz_skoru": skor,
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
        log.info(f"[Telegram yok] {mesaj[:100]}")
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

    haberler = kap_ac(0) + kap_ac(1)  # 2 sayfa ~ son 40-50 açıklama
    log.info(f"{len(haberler)} açıklama alındı")

    yeni_sinyal = 0

    for h in haberler:
        hid     = str(h.get("disclosureIndex") or h.get("id") or "")
        baslik  = h.get("headline") or h.get("title") or ""
        sirket  = h.get("memberTitle") or h.get("company") or ""
        kod     = h.get("stockCode") or h.get("ticker") or ""
        zaman   = h.get("publishedAt") or h.get("date") or ""

        if not hid or hid in gorulmus:
            continue

        gorulmus.add(hid)

        if not on_filtre(baslik):
            continue

        log.info(f"Aday: [{kod}] {baslik[:60]}")
        detay  = haber_detay_cek(hid)
        analiz = gpt_analiz(sirket, kod, baslik, detay)
        skor   = analiz.get("kataliz_skoru", 0)

        log.info(f"  → Skor: {skor}")

        if skor >= KATALIZ_ESIK:
            sinyal = {
                "id":               hid,
                "zaman":            zaman,
                "tarama_zamani":    datetime.utcnow().isoformat() + "Z",
                "sirket":           sirket,
                "kod":              kod,
                "baslik":           baslik,
                "skor":             skor,
                "ozet":             analiz.get("ozet", ""),
                "anlasma_buyuklugu": analiz.get("anlasma_buyuklugu", ""),
                "tekrarlayan":      analiz.get("tekrarlayan", ""),
                "karsi_taraf":      analiz.get("karsi_taraf", ""),
                "kataliz_tipi":     analiz.get("kataliz_tipi", ""),
                "risk":             analiz.get("risk", ""),
                "url":              f"https://www.kap.org.tr/tr/Bildirim/{hid}",
            }
            sinyaller.insert(0, sinyal)
            telegram_gonder(sinyal_mesaji(sinyal))
            yeni_sinyal += 1
            log.info(f"  ✅ SİNYAL: [{kod}] Skor={skor}")

    # Sadece son 200 sinyali tut
    sinyaller = sinyaller[:200]

    # Log kaydı
    tarama_log.insert(0, {
        "zaman": datetime.utcnow().isoformat() + "Z",
        "taranan": len(haberler),
        "yeni_sinyal": yeni_sinyal,
    })
    tarama_log = tarama_log[:100]

    # Kaydet
    kaydet_json(SIGNALS_FILE, sinyaller)
    kaydet_json(SCANLOG_FILE, tarama_log)
    kaydet_json(SEEN_FILE, list(gorulmus)[-2000:])  # Son 2000 ID sakla

    log.info(f"═══ Tarama Bitti — {yeni_sinyal} yeni sinyal ═══")

if __name__ == "__main__":
    main()
