"""
app.py — KAP Radar Streamlit Dashboard

Çalıştır: streamlit run app.py
Canlı:    Streamlit Cloud'a GitHub repo'yu bağla
"""

import json
import os
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests
import streamlit as st

# ─────────────────────────────────────────────────────────────────────────────
#  Sayfa Ayarları
# ─────────────────────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="KAP Radar",
    page_icon="📡",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ─────────────────────────────────────────────────────────────────────────────
#  Stil
# ─────────────────────────────────────────────────────────────────────────────

st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;600&family=IBM+Plex+Sans:wght@300;400;600&display=swap');

html, body, [class*="css"] {
    font-family: 'IBM Plex Sans', sans-serif;
}

.stApp {
    background-color: #0a0e17;
    color: #e2e8f0;
}

.radar-header {
    font-family: 'IBM Plex Mono', monospace;
    font-size: 2.2rem;
    font-weight: 600;
    color: #00ff88;
    letter-spacing: -1px;
    margin-bottom: 0;
    line-height: 1;
}

.radar-sub {
    font-family: 'IBM Plex Mono', monospace;
    font-size: 0.75rem;
    color: #4a5568;
    letter-spacing: 3px;
    text-transform: uppercase;
    margin-top: 4px;
}

.signal-card {
    background: #0f1623;
    border: 1px solid #1e2d40;
    border-left: 3px solid #00ff88;
    border-radius: 8px;
    padding: 16px 20px;
    margin-bottom: 12px;
    transition: border-color 0.2s;
}

.signal-card.high {
    border-left-color: #ff4757;
}

.signal-card.mid {
    border-left-color: #ffa502;
}

.signal-card.low {
    border-left-color: #2ed573;
}

.signal-ticker {
    font-family: 'IBM Plex Mono', monospace;
    font-size: 1.1rem;
    font-weight: 600;
    color: #00ff88;
}

.signal-company {
    font-size: 0.8rem;
    color: #718096;
    margin-left: 8px;
}

.signal-title {
    font-size: 0.92rem;
    color: #cbd5e0;
    margin: 6px 0 10px;
    line-height: 1.4;
}

.signal-ozet {
    font-size: 0.82rem;
    color: #a0aec0;
    margin-bottom: 10px;
    font-style: italic;
}

.badge {
    display: inline-block;
    padding: 2px 10px;
    border-radius: 20px;
    font-size: 0.72rem;
    font-family: 'IBM Plex Mono', monospace;
    margin-right: 6px;
    font-weight: 600;
}

.badge-skor {
    background: #1a2a1a;
    color: #00ff88;
    border: 1px solid #00ff88;
}

.badge-tip {
    background: #1a1a2e;
    color: #7c83fd;
    border: 1px solid #7c83fd;
}

.badge-buyukluk {
    background: #2a1a0e;
    color: #ffa502;
    border: 1px solid #ffa502;
}

.badge-risk {
    background: #2a0e0e;
    color: #ff6b6b;
    border: 1px solid #ff6b6b;
}

.signal-meta {
    font-size: 0.72rem;
    color: #4a5568;
    font-family: 'IBM Plex Mono', monospace;
    margin-top: 8px;
}

.stat-box {
    background: #0f1623;
    border: 1px solid #1e2d40;
    border-radius: 8px;
    padding: 16px;
    text-align: center;
}

.stat-num {
    font-family: 'IBM Plex Mono', monospace;
    font-size: 2rem;
    font-weight: 600;
    color: #00ff88;
    line-height: 1;
}

.stat-label {
    font-size: 0.72rem;
    color: #4a5568;
    text-transform: uppercase;
    letter-spacing: 1px;
    margin-top: 4px;
}

.scan-status {
    font-family: 'IBM Plex Mono', monospace;
    font-size: 0.75rem;
    color: #4a5568;
    padding: 8px 12px;
    background: #0f1623;
    border-radius: 6px;
    border: 1px solid #1e2d40;
}

.pulse {
    display: inline-block;
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: #00ff88;
    margin-right: 6px;
    animation: pulse 2s infinite;
}

@keyframes pulse {
    0%, 100% { opacity: 1; }
    50% { opacity: 0.3; }
}

a.kap-link {
    color: #7c83fd;
    text-decoration: none;
    font-size: 0.75rem;
    font-family: 'IBM Plex Mono', monospace;
}

div[data-testid="stSidebar"] {
    background-color: #080c14;
    border-right: 1px solid #1e2d40;
}

.stSlider > div > div {
    background-color: #00ff88 !important;
}
</style>
""", unsafe_allow_html=True)

# ─────────────────────────────────────────────────────────────────────────────
#  Veri Yükleme
# ─────────────────────────────────────────────────────────────────────────────

GITHUB_OWNER = os.environ.get("GITHUB_OWNER", "")   # Streamlit secrets'tan
GITHUB_REPO  = os.environ.get("GITHUB_REPO", "")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")   # Opsiyonel, private repo için

@st.cache_data(ttl=300)  # 5 dakikada bir yenile
def veri_yukle():
    # 1. Önce yerel dosyayı dene (local geliştirme)
    yerel = Path("data/signals.json")
    if yerel.exists():
        with open(yerel, encoding="utf-8") as f:
            sinyaller = json.load(f)
        log_dosya = Path("data/scan_log.json")
        scan_log = json.load(open(log_dosya)) if log_dosya.exists() else []
        return sinyaller, scan_log

    # 2. GitHub raw URL'den çek (Streamlit Cloud)
    if GITHUB_OWNER and GITHUB_REPO:
        base = f"https://raw.githubusercontent.com/{GITHUB_OWNER}/{GITHUB_REPO}/main"
        headers = {}
        if GITHUB_TOKEN:
            headers["Authorization"] = f"token {GITHUB_TOKEN}"
        try:
            r1 = requests.get(f"{base}/data/signals.json", headers=headers, timeout=10)
            r2 = requests.get(f"{base}/data/scan_log.json", headers=headers, timeout=10)
            sinyaller = r1.json() if r1.ok else []
            scan_log  = r2.json() if r2.ok else []
            return sinyaller, scan_log
        except Exception as e:
            st.error(f"GitHub'dan veri çekilemedi: {e}")

    return [], []

# ─────────────────────────────────────────────────────────────────────────────
#  Sidebar
# ─────────────────────────────────────────────────────────────────────────────

with st.sidebar:
    st.markdown("""
    <div style="padding: 8px 0 24px">
        <div style="font-family: 'IBM Plex Mono', monospace; 
                    font-size: 1.1rem; color: #00ff88; font-weight: 600;">
            📡 KAP RADAR
        </div>
        <div style="font-size: 0.7rem; color: #4a5568; 
                    letter-spacing: 2px; margin-top: 2px;">
            KATALIZ TARAYICI
        </div>
    </div>
    """, unsafe_allow_html=True)

    st.markdown("**Filtreler**")

    min_skor = st.slider(
        "Minimum Kataliz Skoru",
        min_value=0, max_value=100, value=70, step=5,
        help="Gösterilecek minimum sinyal skoru"
    )

    kataliz_tipleri = st.multiselect(
        "Kataliz Tipi",
        ["Sözleşme", "Sipariş", "Ortaklık", "İhracat", "Diğer"],
        default=["Sözleşme", "Sipariş", "Ortaklık", "İhracat", "Diğer"],
    )

    max_sinyal = st.slider(
        "Gösterilecek Sinyal Sayısı",
        min_value=5, max_value=100, value=30, step=5,
    )

    st.markdown("---")
    st.markdown("**Strateji Parametreleri**")
    st.markdown(f"""
    <div style="font-size: 0.8rem; color: #a0aec0; line-height: 2">
        🎯 Hedef çıkış: <b style="color:#00ff88">+35%</b><br>
        🛑 Stop-loss: <b style="color:#ff4757">-10%</b><br>
        ⏱ Maks süre: <b style="color:#ffa502">48 saat</b><br>
        📊 Min hacim: <b style="color:#7c83fd">3x ort.</b>
    </div>
    """, unsafe_allow_html=True)

    st.markdown("---")
    if st.button("🔄 Veriyi Yenile", use_container_width=True):
        st.cache_data.clear()
        st.rerun()

# ─────────────────────────────────────────────────────────────────────────────
#  Ana Sayfa
# ─────────────────────────────────────────────────────────────────────────────

# Header
col_h1, col_h2 = st.columns([3, 1])
with col_h1:
    st.markdown("""
    <div class="radar-header">KAP RADAR</div>
    <div class="radar-sub">Borsa İstanbul · Kataliz Sinyal Sistemi</div>
    """, unsafe_allow_html=True)

# Veri çek
sinyaller, scan_log = veri_yukle()

# Son tarama bilgisi
with col_h2:
    if scan_log:
        son = scan_log[0]
        son_zaman = son.get("zaman", "")[:16].replace("T", " ")
        st.markdown(f"""
        <div class="scan-status">
            <span class="pulse"></span>
            Son tarama<br>
            <b>{son_zaman} UTC</b><br>
            {son.get('taranan', 0)} açıklama tarandı
        </div>
        """, unsafe_allow_html=True)

st.markdown("<br>", unsafe_allow_html=True)

# ── İstatistikler ─────────────────────────────────────────────────────────────
bugun = datetime.now(timezone.utc).date()

bugun_sinyaller = [
    s for s in sinyaller
    if s.get("tarama_zamani", "")[:10] == str(bugun)
]

yuksek_skor = [s for s in sinyaller if s.get("skor", 0) >= 85]

col1, col2, col3, col4 = st.columns(4)
with col1:
    st.markdown(f"""
    <div class="stat-box">
        <div class="stat-num">{len(sinyaller)}</div>
        <div class="stat-label">Toplam Sinyal</div>
    </div>""", unsafe_allow_html=True)

with col2:
    st.markdown(f"""
    <div class="stat-box">
        <div class="stat-num" style="color:#ffa502">{len(bugun_sinyaller)}</div>
        <div class="stat-label">Bugünkü Sinyal</div>
    </div>""", unsafe_allow_html=True)

with col3:
    st.markdown(f"""
    <div class="stat-box">
        <div class="stat-num" style="color:#ff4757">{len(yuksek_skor)}</div>
        <div class="stat-label">Yüksek Skor (85+)</div>
    </div>""", unsafe_allow_html=True)

with col4:
    toplam_tarama = sum(l.get("taranan", 0) for l in scan_log)
    st.markdown(f"""
    <div class="stat-box">
        <div class="stat-num" style="color:#7c83fd">{toplam_tarama}</div>
        <div class="stat-label">Toplam Taranan</div>
    </div>""", unsafe_allow_html=True)

st.markdown("<br>", unsafe_allow_html=True)

# ── Sinyal Listesi ────────────────────────────────────────────────────────────

st.markdown(f"""
<div style="font-family:'IBM Plex Mono',monospace; font-size:0.8rem; 
            color:#4a5568; margin-bottom:12px; letter-spacing:1px;">
    ── SİNYALLER  ·  min skor {min_skor}  ·  {len(kataliz_tipleri)} tip seçili ──
</div>
""", unsafe_allow_html=True)

# Filtrele
filtrelenmis = [
    s for s in sinyaller
    if s.get("skor", 0) >= min_skor
    and s.get("kataliz_tipi", "Diğer") in kataliz_tipleri
][:max_sinyal]

if not filtrelenmis:
    st.markdown("""
    <div style="text-align:center; padding:60px; color:#4a5568; 
                font-family:'IBM Plex Mono',monospace;">
        📭 Henüz sinyal yok.<br>
        <small>Sistem her 15 dakikada KAP'ı taramaktadır.</small>
    </div>
    """, unsafe_allow_html=True)
else:
    for s in filtrelenmis:
        skor      = s.get("skor", 0)
        seviye    = "high" if skor >= 85 else "mid" if skor >= 70 else "low"
        risk      = s.get("risk", "Yok")
        risk_html = (
            f'<span class="badge badge-risk">⚠ {risk}</span>'
            if risk and risk.lower() not in ("yok", "none", "")
            else ""
        )

        st.markdown(f"""
        <div class="signal-card {seviye}">
            <div>
                <span class="signal-ticker">{s.get('kod','?')}</span>
                <span class="signal-company">{s.get('sirket','')}</span>
            </div>
            <div class="signal-title">{s.get('baslik','')}</div>
            <div class="signal-ozet">{s.get('ozet','')}</div>
            <div>
                <span class="badge badge-skor">▲ {skor}/100</span>
                <span class="badge badge-tip">{s.get('kataliz_tipi','')}</span>
                <span class="badge badge-buyukluk">💰 {s.get('anlasma_buyuklugu','?')}</span>
                {risk_html}
            </div>
            <div class="signal-meta">
                🕐 {s.get('zaman','')}  ·  
                🤝 {s.get('karsi_taraf','?')}  ·  
                🔄 {s.get('tekrarlayan','?')}  ·  
                <a class="kap-link" href="{s.get('url','#')}" target="_blank">
                    KAP'ta Aç →
                </a>
            </div>
        </div>
        """, unsafe_allow_html=True)

# ── Tarama Geçmişi ────────────────────────────────────────────────────────────

if scan_log:
    with st.expander("📋 Tarama Geçmişi"):
        for log_entry in scan_log[:20]:
            z = log_entry.get("zaman", "")[:16].replace("T", " ")
            t = log_entry.get("taranan", 0)
            y = log_entry.get("yeni_sinyal", 0)
            renk = "#00ff88" if y > 0 else "#4a5568"
            st.markdown(
                f'<span style="font-family:\'IBM Plex Mono\',monospace; '
                f'font-size:0.75rem; color:{renk};">'
                f'{z} UTC  —  {t} taranan  —  '
                f'<b>{y} sinyal</b></span>',
                unsafe_allow_html=True
            )
