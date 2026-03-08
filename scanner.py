"""
scanner.py — GitHub Actions'ta çalışan KAP tarayıcı
Kaynak: finans.mynet.com/borsa/kaphaberleri/
Filtreden geçen adaylar için detay sayfası fetch edilir, tam metin skorlanır.
"""

import os
import json
import hashlib
import logging
import requests
import re
import time
from datetime import datetime, timezone
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

# Bildirim tipleri — direkt elenenler
NEGATIF_TIPLER = [
    "finansal rapor", "faaliyet raporu", "sorumluluk beyanı",
    "kurumsal yönetim", "sürdürülebilirlik", "katılım finansı",
    "bağımsız denetim", "piyasa yapıcılığı", "sermaye artırımından",
    "temettü", "genel kurul", "bilgi formu", "uyum raporu",
]

# Bildirim tipleri — detay sayfası fetch edilecekler
POZITIF_TIPLER = [
    "özel durum", "önemli sözleşme", "önemli anlaşma",
    "ihracat", "satış", "sipariş",
]

# İçerik anahtar kelimeleri
KATALIZ_KELIMELER = {
    "milyon": 15, "milyar": 25, "usd": 10, "eur": 10, "dolar": 8,
    "yurt dışı": 12, "uluslararası": 8, "ihracat": 10,
    "uzun vadeli": 8, "çok yıllık": 10, "münhasır": 12,
    "ihale kazandı": 22, "ihaleyi kazandı": 22,
    "büyük sipariş": 18, "dev anlaşma": 20,
    "sipariş aldı": 18, "sözleşme imzalandı": 18,
    "anlaşma sağlandı": 15, "tedarik anlaşması": 14,
    "satış sözleşmesi": 14, "lisans anlaşması": 12,
    "ortaklık anlaşması": 12, "joint venture": 12,
    "konsorsiyum": 10, "ihale": 8,
}
NEGATIF_KELIMELER = {
    "ön anlaşma": -15, "niyet mektubu": -12,
    "mou": -10, "görüşme": -8, "değerlendirilmekte": -8,
    "protokol imzalandı": -5,
}

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
#  Tarih
# ─────────────────────────────────────────────────────────────────────────────

AYLAR = {
    "Oca": 1, "Şub": 2, "Mar": 3, "Nis": 4, "May": 5, "Haz": 6,
    "Tem": 7, "Ağu": 8, "Eyl": 9, "Eki": 10, "Kas": 11, "Ara": 12,
}

def mynet_tarih_parse(tarih_str: str):
    try:
        m = re.search(
            r"(\d{1,2})\s+(Oca|Şub|Mar|Nis|May|Haz|Tem|Ağu|Eyl|Eki|Kas|Ara)\s+(\d{4})\s+(\d{2}):(\d{2})",
            tarih_str
        )
        if m:
            gun, ay_str, yil, saat, dakika = m.groups()
            return datetime(int(yil), AYLAR[ay_str], int(gun),
                            int(saat), int(dakika), tzinfo=timezone.utc)
    except:
        pass
    return None

def haber_taze_mi(tarih_str: str) -> bool:
    if not tarih_str:
        return True
    dt = mynet_tarih_parse(tarih_str)
    if dt is None:
        return True
    fark_saat = (datetime.now(timezone.utc) - dt).total_seconds() / 3600
    if fark_saat > MAX_HABER_YAS_SAAT:
        log.info(f"  ⏭ Eski haber ({int(fark_saat)}sa)")
        return False
    return True

# ─────────────────────────────────────────────────────────────────────────────
#  Mynet KAP Listesi
# ─────────────────────────────────────────────────────────────────────────────

def mynet_kap_cek() -> list:
    url = "https://finans.mynet.com/borsa/kaphaberleri/"
    haberler = []
    try:
        r = requests.get(url, headers=HEADERS, timeout=20)
        log.info(f"Mynet KAP status: {r.status_code}, uzunluk: {len(r.text)}")
        soup = BeautifulSoup(r.text, "html.parser")
        linkler = soup.find_all("a", href=re.compile(r"/borsa/haberdetay/"))
        log.info(f"Bulunan KAP haberi linki: {len(linkler)}")

        for link in linkler:
            metin = link.get_text(strip=True)
            if not metin or len(metin) < 10:
                continue

            href = link.get("href", "")
            tam_url = f"https://finans.mynet.com{href}" if href.startswith("/") else href

            # Hisse kodu: ***KDMR***
            kod_match = re.search(r"\*\*\*([A-Z0-9]{2,8})\*\*\*", metin)
            hisse_kodu = kod_match.group(1) if kod_match else ""

            # Birden fazla *** varsa ilkini al
            if not hisse_kodu:
                continue

            # Bildirim tipi: sonundaki parantez
            tip_match = re.search(r"\(([^)]+)\)\s*$", metin)
            bildirim_tipi = tip_match.group(1).strip() if tip_match else ""

            # Şirket adı
            sirket = re.sub(r"\*\*\*[A-Z0-9]+\*\*\*\s*", "", metin)
            sirket = re.sub(r"\s*\([^)]+\)\s*$", "", sirket).strip()
            sirket = re.sub(r"\s+", " ", sirket)

            # Tarih: üst elementden
            tarih_str = ""
            for parent in [link.parent, link.parent.parent if link.parent else None]:
                if parent:
                    t = re.search(
                        r"\d{1,2}\s+(?:Oca|Şub|Mar|Nis|May|Haz|Tem|Ağu|Eyl|Eki|Kas|Ara)\s+\d{4}\s+\d{2}:\d{2}",
                        parent.get_text()
                    )
                    if t:
                        tarih_str = t.group(0)
                        break

            hid = hashlib.md5(tam_url.encode("utf-8")).hexdigest()[:12]
            haberler.append({
                "id": hid, "baslik": metin.strip(),
                "sirket": sirket, "kod": hisse_kodu,
                "bildirim_tipi": bildirim_tipi,
                "url": tam_url, "zaman": tarih_str,
                "icerik": "",
            })

    except Exception as e:
        log.error(f"Mynet KAP hatası: {e}")

    return haberler

# ─────────────────────────────────────────────────────────────────────────────
#  Detay Sayfası Fetch
# ─────────────────────────────────────────────────────────────────────────────

def detay_cek(url: str) -> str:
    """Mynet haber detay sayfasından KAP bildirim metnini çeker."""
    try:
        time.sleep(0.5)  # rate limit
        r = requests.get(url, headers=HEADERS, timeout=15)
        soup = BeautifulSoup(r.text, "html.parser")

        # Mynet detay sayfasında haber içeriği
        icerik_div = (
            soup.find("div", class_=re.compile(r"news-detail|haber-icerik|article|content", re.I))
            or soup.find("article")
            or soup.find("div", id=re.compile(r"content|icerik", re.I))
        )

        if icerik_div:
            metin = icerik_div.get_text(separator=" ", strip=True)
            return metin[:2000]

        # Fallback: tüm p tagları
        paragraflar = soup.find_all("p")
        return " ".join(p.get_text(strip=True) for p in paragraflar[:10])[:2000]

    except Exception as e:
        log.warning(f"Detay fetch hatası ({url}): {e}")
        return ""

# ─────────────────────────────────────────────────────────────────────────────
#  Filtre
# ─────────────────────────────────────────────────────────────────────────────

def on_filtre(bildirim_tipi: str, zaman: str = "") -> bool:
    if not haber_taze_mi(zaman):
        return False

    tip = bildirim_tipi.lower()

    for t in NEGATIF_TIPLER:
        if t in tip:
            return False

    for t in POZITIF_TIPLER:
        if t in tip:
            return True

    return False

# ─────────────────────────────────────────────────────────────────────────────
#  Analiz
# ─────────────────────────────────────────────────────────────────────────────

GPT_SISTEM = """Sen bir BIST uzman analistisin. KAP bildirim içeriklerini analiz ederek
kısa vadeli fiyat katalizi yaratabilecekleri 0-100 arasında skorluyorsun.
SADECE JSON yanıt ver, başka metin ekleme."""

GPT_PROMPT = """Hisse: {kod} — {sirket}
Bildirim tipi: {bildirim_tipi}
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

def gpt_analiz(h: dict) -> dict:
    if not OPENAI_AVAILABLE or not OPENAI_API_KEY:
        return kural_analiz(h)
    try:
        client = OpenAI(api_key=OPENAI_API_KEY)
        icerik = h.get("icerik", "") or h.get("baslik", "")
        resp = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": GPT_SISTEM},
                {"role": "user", "content": GPT_PROMPT.format(
                    kod=h.get("kod", ""),
                    sirket=h.get("sirket", ""),
                    bildirim_tipi=h.get("bildirim_tipi", ""),
                    icerik=icerik[:1500],
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
    # Hem başlık hem içerik üzerinde skor
    metin = (h.get("icerik", "") + " " + h.get("baslik", "")).lower()
    tip   = h.get("bildirim_tipi", "").lower()
    skor  = 45  # base

    # Bildirim tipine göre baz
    if "özel durum" in tip:
        skor += 10
    if "önemli" in tip:
        skor += 15

    for k, v in KATALIZ_KELIMELER.items():
        if k in metin:
            skor += v

    for k, v in NEGATIF_KELIMELER.items():
        if k in metin:
            skor += v

    ozet = h.get("icerik", h.get("baslik", ""))[:150]
    return {
        "kataliz_skoru": max(0, min(100, skor)),
        "ozet": ozet,
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
    emoji = "🔴" if h["skor"] >= 85 else "🟡"
    return (
        f"{emoji} *KATALİZ SİNYALİ* `{h.get('kod','')}`\n\n"
        f"🏢 {h.get('sirket', '')}\n"
        f"📋 {h.get('bildirim_tipi', '')}\n\n"
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
    aday_sayisi = 0

    for h in haberler:
        hid = h.get("id", "")
        if not hid or hid in gorulmus:
            continue
        gorulmus.add(hid)

        if not on_filtre(h.get("bildirim_tipi", ""), h.get("zaman", "")):
            continue

        aday_sayisi += 1
        log.info(f"Aday [{h.get('kod','')}]: {h.get('bildirim_tipi','')} — {h.get('sirket','')[:40]}")

        # Detay sayfasını çek
        icerik = detay_cek(h["url"])
        h["icerik"] = icerik
        log.info(f"  Detay: {len(icerik)} karakter")

        analiz = gpt_analiz(h)
        skor   = analiz.get("kataliz_skoru", 0)
        log.info(f"  → Skor: {skor}")

        if skor >= KATALIZ_ESIK:
            sinyal = {
                "id": hid,
                "zaman": h.get("zaman", ""),
                "tarama_zamani": datetime.utcnow().isoformat() + "Z",
                "sirket": h.get("sirket", ""),
                "kod": h.get("kod", ""),
                "bildirim_tipi": h.get("bildirim_tipi", ""),
                "baslik": h.get("baslik", ""),
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
            log.info(f"  ✅ SİNYAL: [{h.get('kod','')}] Skor={skor}")

    log.info(f"Aday sayısı: {aday_sayisi}, Yeni sinyal: {yeni_sinyal}")

    sinyaller  = sinyaller[:200]
    tarama_log.insert(0, {
        "zaman": datetime.utcnow().isoformat() + "Z",
        "taranan": len(haberler),
        "aday": aday_sayisi,
        "yeni_sinyal": yeni_sinyal,
    })
    tarama_log = tarama_log[:100]

    kaydet_json(SIGNALS_FILE, sinyaller)
    kaydet_json(SCANLOG_FILE, tarama_log)
    kaydet_json(SEEN_FILE, list(gorulmus)[-2000:])
    log.info(f"═══ Tarama Bitti — {yeni_sinyal} yeni sinyal ═══")

if __name__ == "__main__":
    main()