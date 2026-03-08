"""
scanner.py — GitHub Actions'ta çalışan KAP tarayıcı
Google News RSS üzerinden KAP haberlerini takip eder.
KAP'ın kendi sitesi GitHub IP'lerini engellediği için
Google News RSS alternatif kaynak olarak kullanılır.
"""

import os
import json
import hashlib
import logging
import requests
import xml.etree.ElementTree as ET
from datetime import datetime
from html import unescape
import re

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
    "joint venture", "konsorsiyum", "ihale kazandı", "ihaleyi kazandı",
    "satış sözleşmesi", "lisans anlaşması", "yurt dışı sipariş",
    "özel durum açıklaması", "ozel durum",
]
NEGATIF = [
    "faaliyet raporu", "bilanço", "bilanco", "genel kurul",
    "temettü", "temettu", "bağımsız denetim", "yönetim kurulu üye",
    "sermaye artırımı", "hisse geri alım", "finansal rapor",
]

# Google News RSS sorgu listeleri — KAP haberleri için
RSS_SORGULAR = [
    "KAP+sözleşme+BIST",
    "KAP+anlaşma+borsa",
    "KAP+ihracat+sipariş+hisse",
    "KAP+özel+durum+açıklaması",
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
#  Google News RSS
# ─────────────────────────────────────────────────────────────────────────────

def google_news_cek(sorgu: str) -> list:
    url = f"https://news.google.com/rss/search?q={sorgu}&hl=tr&gl=TR&ceid=TR:tr"
    haberler = []
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; FeedFetcher-Google)"
    }
    try:
        r = requests.get(url, headers=headers, timeout=15)
        log.info(f"RSS sorgu '{sorgu}': status={r.status_code}, uzunluk={len(r.text)}")

        root = ET.fromstring(r.content)
        items = root.findall(".//item")
        log.info(f"  → {len(items)} haber bulundu")

        for item in items:
            baslik = item.findtext("title") or ""
            link   = item.findtext("link") or ""
            tarih  = item.findtext("pubDate") or ""
            kaynak = item.findtext("source") or ""

            # HTML taglarını temizle
            baslik = unescape(re.sub(r'<[^>]+>', '', baslik))

            # Benzersiz ID üret
            hid = hashlib.md5(link.encode("utf-8")).hexdigest()[:12]

            haberler.append({
                "id":     hid,
                "baslik": baslik.strip(),
                "url":    link,
                "zaman":  tarih,
                "sirket": kaynak,
                "kod":    "",
            })

    except ET.ParseError as e:
        log.error(f"RSS parse hatası ({sorgu}): {e}")
    except Exception as e:
        log.error(f"RSS hatası ({sorgu}): {e}")

    return haberler

def tum_haberleri_cek() -> list:
    tum = []
    gorulmus_basliklar = set()

    for sorgu in RSS_SORGULAR:
        haberler = google_news_cek(sorgu)
        for h in haberler:
            # Duplicate başlık kontrolü
            if h["baslik"] not in gorulmus_basliklar:
                gorulmus_basliklar.add(h["baslik"])
                tum.append(h)

    log.info(f"Toplam benzersiz haber: {len(tum)}")
    return tum

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
    # KAP'tan gelen haber ama pozitif kelime yoksa da tara
    if "kap" in metin and any(k in metin for k in ["milyon", "milyar", "usd", "eur"]):
        return True
    return False

# ─────────────────────────────────────────────────────────────────────────────
#  Analiz
# ─────────────────────────────────────────────────────────────────────────────

GPT_SISTEM = """Sen bir BIST uzman analistisin. Haber başlıklarını analiz ederek
kısa vadeli fiyat katalizi yaratabilecek haberleri 0-100 arasında skorluyorsun.
SADECE JSON yanıt ver, başka metin ekleme."""

GPT_PROMPT = """Haber başlığı: {baslik}
Kaynak: {kaynak}

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

def gpt_analiz(baslik, kaynak="") -> dict:
    if not OPENAI_AVAILABLE or not OPENAI_API_KEY:
        return kural_analiz(baslik)
    try:
        client = OpenAI(api_key=OPENAI_API_KEY)
        resp = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": GPT_SISTEM},
                {"role": "user", "content": GPT_PROMPT.format(
                    baslik=baslik, kaynak=kaynak
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
    for k, v in {
        "milyon": 15, "milyar": 25, "usd": 10, "eur": 10,
        "yurt dışı": 10, "uluslararası": 8, "uzun vadeli": 8,
        "çok yıllık": 10, "münhasır": 12, "ihale kazandı": 20,
        "büyük sipariş": 18, "dev anlaşma": 20,
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
    emoji = "🔴" if h["skor"] >= 85 else "🟡" if h["skor"] >= 70 else "⚪"
    return (
        f"{emoji} *KATALİZ SİNYALİ*\n\n"
        f"📰 {h['baslik']}\n\n"
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

    haberler    = tum_haberleri_cek()
    yeni_sinyal = 0

    for h in haberler:
        hid    = h.get("id", "")
        baslik = h.get("baslik", "")
        kaynak = h.get("sirket", "")

        if not baslik or hid in gorulmus:
            continue
        gorulmus.add(hid)

        if not on_filtre(baslik):
            continue

        log.info(f"Aday: {baslik[:70]}")
        analiz = gpt_analiz(baslik, kaynak)
        skor   = analiz.get("kataliz_skoru", 0)
        log.info(f"  → Skor: {skor}")

        if skor >= KATALIZ_ESIK:
            sinyal = {
                "id": hid,
                "zaman": h.get("zaman", ""),
                "tarama_zamani": datetime.utcnow().isoformat() + "Z",
                "sirket": kaynak,
                "kod": "",
                "baslik": baslik,
                "skor": skor,
                "ozet": analiz.get("ozet", ""),
                "anlasma_buyuklugu": analiz.get("anlasma_buyuklugu", ""),
                "tekrarlayan": analiz.get("tekrarlayan", ""),
                "karsi_taraf": analiz.get("karsi_taraf", ""),
                "kataliz_tipi": analiz.get("kataliz_tipi", ""),
                "risk": analiz.get("risk", ""),
                "url": h.get("url", ""),
            }
            sinyaller.insert(0, sinyal)
            telegram_gonder(sinyal_mesaji(sinyal))
            yeni_sinyal += 1
            log.info(f"  ✅ SİNYAL: Skor={skor}")

    sinyaller  = sinyaller[:200]
    tarama_log.insert(0, {
        "zaman": datetime.utcnow().isoformat() + "Z",
        "taranan": len(haberler),
        "yeni_sinyal": yeni_sinyal,
    })
    tarama_log = tarama_log[:100]

    kaydet_json(SIGNALS_FILE, sinyaller)
    kaydet_json(SCANLOG_FILE, tarama_log)
    kaydet_json(SEEN_FILE, list(gorulmus)[-2000:])
    log.info(f"═══ Tarama Bitti — {yeni_sinyal} yeni sinyal ═══")

if __name__ == "__main__":
    main()