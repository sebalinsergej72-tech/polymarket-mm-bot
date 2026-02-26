import streamlit as st
import time
import threading
import queue
import os
import requests
from dotenv import load_dotenv

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY, SELL

load_dotenv()

st.set_page_config(page_title="Polymarket MM Bot", layout="wide")

if "running" not in st.session_state:
    st.session_state.running = False
if "log_queue" not in st.session_state:
    st.session_state.log_queue = queue.Queue(maxsize=300)

st.title("🚀 Polymarket Market-Making Bot")
st.markdown("**Реальная торговля • 24/7 • Polymarket API**")

PRIVATE_KEY = os.getenv("PRIVATE_KEY")
if not PRIVATE_KEY:
    st.error("❌ PRIVATE_KEY не найден!")
    st.stop()

HOST = "https://clob.polymarket.com"
CHAIN_ID = 137
GAMMA_URL = "https://gamma-api.polymarket.com"

@st.cache_resource(show_spinner=False)
def get_client():
    client = ClobClient(HOST, key=PRIVATE_KEY, chain_id=CHAIN_ID)
    client.set_api_creds(client.create_or_derive_api_creds())
    return client

client = get_client()

def log(text: str):
    st.session_state.log_queue.put(f"[{time.strftime('%H:%M:%S')}] {text}")

def bot_loop(order_size: float, spread_bps: int, refresh_sec: int, max_markets: int):
    log("🚀 Бот запущен в облаке!")
    while st.session_state.running:
        try:
            client.cancel_all()
            log("✅ Все ордера отменены")

            # ←←← ИСПРАВЛЕНИЕ: используем /events (самый стабильный способ)
            r = requests.get(f"{GAMMA_URL}/events", params={
                "active": "true",
                "closed": "false",
                "order": "volume_24hr",
                "ascending": "false",
                "limit": "50"
            }, timeout=15)

            data = r.json() if r.ok else []
            log(f"📥 Получено {len(data)} событий с Gamma API")

            markets = []
            for event in data:
                for m in event.get("markets", []):
                    if m.get("clobTokenIds") and len(m["clobTokenIds"]) == 2:   # только бинарные рынки
                        markets.append({
                            "condition_id": m["conditionId"],
                            "token_yes": m["clobTokenIds"][0],
                            "slug": m.get("slug", "Unknown")[:40],
                            "volume": m.get("volume_24hr", 0)
                        })

            markets = sorted(markets, key=lambda x: x["volume"], reverse=True)[:max_markets]
            log(f"📊 Найдено {len(markets)} активных бинарных рынков")

            if len(markets) == 0:
                log("⚠️ Нет доступных рынков. Возможно временная проблема API.")
                time.sleep(refresh_sec)
                continue

            for m in markets:
                try:
                    mid = client.get_midpoint(m["token_yes"])
                    spread = spread_bps / 10000.0
                    p_buy = max(0.01, round(mid - spread/2, 4))
                    p_sell = min(0.99, round(mid + spread/2, 4))

                    info = requests.get(f"{GAMMA_URL}/markets/{m['condition_id']}", timeout=10).json()
                    tick = str(info.get("minimumTickSize", "0.01"))
                    neg = info.get("negRisk", False)

                    for side, price in [(BUY, p_buy), (SELL, p_sell)]:
                        order = OrderArgs(token_id=m["token_yes"], price=price, size=order_size, side=side, order_type=OrderType.GTC)
                        client.create_and_post_order(order, {"tick_size": tick, "neg_risk": neg})
                        log(f"✅ {'BUY' if side == BUY else 'SELL'} {m['slug']} @ {price}")
                except Exception as e:
                    log(f"⚠️ Ошибка рынка {m['slug']}: {str(e)[:60]}")

            log(f"⏳ Следующий цикл через {refresh_sec} сек...")
            time.sleep(refresh_sec)

        except Exception as e:
            log(f"❌ Критическая ошибка: {e}")
            time.sleep(5)

    log("🛑 Бот остановлен")

# ================== UI ==================
with st.sidebar:
    st.header("⚙️ Настройки")
    order_size = st.slider("Размер ордера (USDC)", 5, 100, 10, 1)
    spread_bps = st.slider("Спред (bps)", 10, 40, 20, 1)
    refresh_sec = st.slider("Интервал (сек)", 5, 30, 8, 1)
    max_markets = st.slider("Макс. рынков", 1, 8, 3, 1)

    col1, col2 = st.columns(2)
    if col1.button("▶ Запустить бота", type="primary", use_container_width=True):
        if not st.session_state.running:
            st.session_state.running = True
            thread = threading.Thread(target=bot_loop, args=(order_size, spread_bps, refresh_sec, max_markets), daemon=True)
            thread.start()

    if col2.button("⏹ Остановить", type="secondary", use_container_width=True):
        st.session_state.running = False
        try:
            client.cancel_all()
        except:
            pass

st.subheader("Статус: " + ("🟢 **БОТ РАБОТАЕТ**" if st.session_state.running else "🔴 Остановлен"))

log_area = st.empty()
while True:
    logs = []
    while not st.session_state.log_queue.empty():
        logs.append(st.session_state.log_queue.get())
    if logs:
        log_area.code("\n".join(logs[-150:]), language=None)
    time.sleep(0.4)
    st.rerun()
