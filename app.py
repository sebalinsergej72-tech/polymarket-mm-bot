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
st.markdown("**Реальная торговля • 24/7 • Polymarket API**")

PRIVATE_KEY = os.getenv("PRIVATE_KEY")
if not PRIVATE_KEY:
    st.error("❌ PRIVATE_KEY не найден! Добавь его в Variables → Redeploy")
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

# ГЛОБАЛЬНЫЕ ПЕРЕМЕННЫЕ (объявляем ПЕРЕД функциями!)
bot_running = False
log_queue: queue.Queue = queue.Queue(maxsize=300)

def log(text: str):
    log_queue.put(f"[{time.strftime('%H:%M:%S')}] {text}")

def bot_loop(order_size: float, spread_bps: int, refresh_sec: int, max_markets: int):
    global bot_running
    log("🚀 Бот запущен в облаке!")

    while bot_running:
        try:
            client.cancel_all()
            log("✅ Все ордера отменены")

            r = requests.get(f"{GAMMA_URL}/markets", params={
                "active": "true",
                "closed": "false",
                "order": "volume_24hr",
                "ascending": "false",
                "limit": str(max_markets * 6)
            }, timeout=15)

            markets_data = r.json() if r.ok else []

            markets = []
            for m in markets_data:
                if isinstance(m, dict) and m.get("clobTokenIds") and len(m.get("clobTokenIds", [])) == 2:
                    markets.append({
                        "condition_id": m["conditionId"],
                        "token_yes": m["clobTokenIds"][0],
                        "slug": m.get("slug", "Unknown")[:40],
                        "volume": m.get("volume_24hr", 0)
                    })

            markets = sorted(markets, key=lambda x: x["volume"], reverse=True)[:max_markets]
            log(f"📊 Найдено {len(markets)} рынков")

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
                    log(f"⚠️ Ошибка на рынке {m['slug']}: {str(e)[:70]}")

            log(f"⏳ Следующий цикл через {refresh_sec} сек...")
            time.sleep(refresh_sec)

        except Exception as e:
            log(f"❌ Критическая ошибка: {e}")
            time.sleep(5)

    log("🛑 Бот остановлен")

# ================== ИНТЕРФЕЙС ==================
with st.sidebar:
    st.header("⚙️ Настройки")
    order_size = st.slider("Размер ордера (USDC)", 10, 500, 10, 1)   # по умолчанию 10 для теста
    spread_bps = st.slider("Спред (bps)", 5, 60, 20, 1)
    refresh_sec = st.slider("Интервал (сек)", 5, 30, 8, 1)
    max_markets = st.slider("Макс. рынков", 1, 12, 3, 1)            # по умолчанию 3

    col1, col2 = st.columns(2)
    if col1.button("▶ Запустить бота", type="primary", use_container_width=True):
        global bot_running
        if not bot_running:
            bot_running = True
            thread = threading.Thread(target=bot_loop, args=(order_size, spread_bps, refresh_sec, max_markets), daemon=True)
            thread.start()

    if col2.button("⏹ Остановить", type="secondary", use_container_width=True):
        global bot_running
        bot_running = False
        try:
            client.cancel_all()
        except:
            pass

st.subheader("Статус: " + ("🟢 **БОТ РАБОТАЕТ**" if bot_running else "🔴 Остановлен"))

# Логи
log_area = st.empty()
while True:
    logs = []
    while not log_queue.empty():
        logs.append(log_queue.get())
    if logs:
        log_area.code("\n".join(logs[-150:]), language=None)
    time.sleep(0.4)
    st.rerun()
