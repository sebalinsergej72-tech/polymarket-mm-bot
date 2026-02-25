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

st.title("🚀 Polymarket Market-Making Bot")
st.markdown("**Реальная торговля 2026 • Только Polymarket API • Работает 24/7**")

# === КЛЮЧИ ===
PRIVATE_KEY = os.getenv("PRIVATE_KEY")
if not PRIVATE_KEY:
    st.error("❌ Добавь PRIVATE_KEY в Variables на Railway!")
    st.stop()

HOST = "https://clob.polymarket.com"
CHAIN_ID = 137
GAMMA_URL = "https://gamma-api.polymarket.com"

@st.cache_resource
def get_client():
    client = ClobClient(HOST, key=PRIVATE_KEY, chain_id=CHAIN_ID)
    client.set_api_creds(client.create_or_derive_api_creds())
    return client

client = get_client()

# === SESSION ===
if "running" not in st.session_state:
    st.session_state.running = False
if "log_queue" not in st.session_state:
    st.session_state.log_queue = queue.Queue(maxsize=200)

def log(text):
    st.session_state.log_queue.put(f"[{time.strftime('%H:%M:%S')}] {text}")

def bot_loop(order_size, spread_bps, refresh_sec, max_markets):
    while st.session_state.running:
        try:
            client.cancel_all()
            log("✅ Все ордера отменены")

            # Топ рынки
            params = {"active": "true", "closed": "false", "order": "volume_24hr", "ascending": "false", "limit": str(max_markets*2)}
            markets = requests.get(f"{GAMMA_URL}/events", params=params).json()

            active = []
            for e in markets:
                for m in e.get("markets", []):
                    if len(m.get("clobTokenIds", [])) == 2:
                        active.append({
                            "condition_id": m["conditionId"],
                            "token_yes": m["clobTokenIds"][0],
                            "slug": m.get("slug", "Unknown"),
                            "volume": m.get("volume_24hr", 0)
                        })

            active = sorted(active, key=lambda x: x["volume"], reverse=True)[:max_markets]
            log(f"📊 Найдено {len(active)} рынков для MM")

            for m in active:
                mid = client.get_midpoint(m["token_yes"])
                spread = spread_bps / 10000.0
                p_buy = max(0.01, round(mid - spread/2, 4))
                p_sell = min(0.99, round(mid + spread/2, 4))

                info = requests.get(f"{GAMMA_URL}/markets/{m['condition_id']}").json()
                tick = str(info.get("minimumTickSize", "0.01"))
                neg = info.get("negRisk", False)

                for side, price in [(BUY, p_buy), (SELL, p_sell)]:
                    order = OrderArgs(token_id=m["token_yes"], price=price, size=order_size, side=side, order_type=OrderType.GTC)
                    try:
                        resp = client.create_and_post_order(order, {"tick_size": tick, "neg_risk": neg})
                        log(f"✅ {'BUY' if side==BUY else 'SELL'} {m['slug'][:25]} @ {price}")
                    except Exception as e:
                        log(f"⚠️ {str(e)[:80]}")

            log(f"⏳ Следующий цикл через {refresh_sec} сек...")
            time.sleep(refresh_sec)

        except Exception as e:
            log(f"❌ Ошибка: {e}")
            time.sleep(5)

# === UI ===
with st.sidebar:
    st.header("⚙️ Настройки")
    order_size = st.slider("Размер ордера USDC", 10, 500, 50, 5)
    spread_bps = st.slider("Спред (bps)", 5, 60, 15, 1)
    refresh_sec = st.slider("Интервал (сек)", 5, 30, 8, 1)
    max_markets = st.slider("Макс. рынков", 1, 12, 5, 1)

    col1, col2 = st.columns(2)
    if col1.button("▶ Запустить бота", type="primary", use_container_width=True):
        if not st.session_state.running:
            st.session_state.running = True
            threading.Thread(target=bot_loop, args=(order_size, spread_bps, refresh_sec, max_markets), daemon=True).start()
            log("🚀 Бот запущен в облаке!")

    if col2.button("⏹ Остановить", type="secondary", use_container_width=True):
        st.session_state.running = False
        log("🛑 Бот остановлен")
        try:
            client.cancel_all()
        except:
            pass

st.subheader("Статус: " + ("🟢 **РАБОТАЕТ**" if st.session_state.running else "🔴 Остановлен"))

# Логи
log_area = st.empty()
while True:
    logs = []
    while not st.session_state.log_queue.empty():
        logs.append(st.session_state.log_queue.get())
    if logs:
        log_area.code("\n".join(logs[-80:]), language=None)
    time.sleep(0.5)
    st.rerun()
