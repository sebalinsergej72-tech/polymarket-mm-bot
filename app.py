import math
import os
import queue
import re
import json
import threading
import time
from collections import deque
from typing import Any, Dict, Optional

import requests
import streamlit as st
from dotenv import load_dotenv
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import AssetType, BalanceAllowanceParams, OrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY, SELL
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

load_dotenv()

st.set_page_config(page_title="Polymarket MM Bot", layout="wide")

HOST = "https://clob.polymarket.com"
CHAIN_ID = 137
GAMMA_URL = "https://gamma-api.polymarket.com"
DATA_API_URL = "https://data-api.polymarket.com"

PRIVATE_KEY_RE = re.compile(r"^(0x)?[0-9a-fA-F]{64}$")
ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")

if "running" not in st.session_state:
    st.session_state.running = False
if "log_queue" not in st.session_state:
    st.session_state.log_queue = queue.Queue(maxsize=800)
if "log_buffer" not in st.session_state:
    st.session_state.log_buffer = deque(maxlen=240)
if "metrics_queue" not in st.session_state:
    st.session_state.metrics_queue = queue.Queue(maxsize=50)
if "runtime_metrics" not in st.session_state:
    st.session_state.runtime_metrics = {}
if "worker_thread" not in st.session_state:
    st.session_state.worker_thread = None
if "stop_event" not in st.session_state:
    st.session_state.stop_event = threading.Event()


st.title("Polymarket Market-Making Bot")
st.markdown("**Реальная торговля • 24/7 • Polymarket CLOB**")


def is_valid_private_key(value: str) -> bool:
    return bool(value and PRIVATE_KEY_RE.fullmatch(value.strip()))


def is_valid_address(value: str) -> bool:
    return bool(value and ADDRESS_RE.fullmatch(value.strip()))


def parse_signature_type(raw_value: str) -> Optional[int]:
    try:
        value = int(raw_value)
    except (TypeError, ValueError):
        return None
    return value if value in (0, 1, 2) else None


def parse_bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def to_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return default


PRIVATE_KEY = (os.getenv("PRIVATE_KEY") or "").strip()
if not is_valid_private_key(PRIVATE_KEY):
    st.error("❌ PRIVATE_KEY отсутствует или имеет неверный формат. Ожидается 64 hex (с optional 0x).")
    st.stop()

raw_signature_type = (os.getenv("SIGNATURE_TYPE") or "0").strip()
SIGNATURE_TYPE = parse_signature_type(raw_signature_type)
if SIGNATURE_TYPE is None:
    st.error("❌ SIGNATURE_TYPE должен быть одним из: 0, 1, 2")
    st.stop()

FUNDER_ADDRESS = (os.getenv("FUNDER_ADDRESS") or "").strip() or None
if SIGNATURE_TYPE in (1, 2) and not FUNDER_ADDRESS:
    st.error("❌ Для SIGNATURE_TYPE=1/2 необходимо задать FUNDER_ADDRESS")
    st.stop()
if FUNDER_ADDRESS and not is_valid_address(FUNDER_ADDRESS):
    st.error("❌ FUNDER_ADDRESS имеет неверный формат EVM-адреса")
    st.stop()

WALLET_ADDRESS_ENV = (os.getenv("WALLET_ADDRESS") or "").strip() or None
if WALLET_ADDRESS_ENV and not is_valid_address(WALLET_ADDRESS_ENV):
    st.error("❌ WALLET_ADDRESS имеет неверный формат EVM-адреса")
    st.stop()

ENABLE_SELL_INVENTORY_GUARD = parse_bool_env("ENABLE_SELL_INVENTORY_GUARD", True)


@st.cache_resource(show_spinner=False)
def get_client() -> ClobClient:
    kwargs: Dict[str, Any] = {
        "key": PRIVATE_KEY,
        "chain_id": CHAIN_ID,
        "signature_type": SIGNATURE_TYPE,
    }
    if FUNDER_ADDRESS:
        kwargs["funder"] = FUNDER_ADDRESS

    client = ClobClient(HOST, **kwargs)
    client.set_api_creds(client.create_or_derive_api_creds())
    return client


@st.cache_resource(show_spinner=False)
def get_http_session() -> requests.Session:
    session = requests.Session()
    retries = Retry(
        total=4,
        connect=4,
        read=4,
        backoff_factor=0.4,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=frozenset(["GET"]),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retries)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


client = get_client()
http = get_http_session()

RESOLVED_WALLET_ADDRESS = WALLET_ADDRESS_ENV or FUNDER_ADDRESS
if not RESOLVED_WALLET_ADDRESS:
    try:
        maybe_address = client.get_address()
        if is_valid_address(str(maybe_address)):
            RESOLVED_WALLET_ADDRESS = str(maybe_address)
    except Exception:
        RESOLVED_WALLET_ADDRESS = None

if RESOLVED_WALLET_ADDRESS and not is_valid_address(RESOLVED_WALLET_ADDRESS):
    st.error("❌ Не удалось валидно определить адрес кошелька")
    st.stop()


st.caption(
    f"Wallet: `{RESOLVED_WALLET_ADDRESS or 'unknown'}` | "
    f"Signature type: `{SIGNATURE_TYPE}` | Chain: `{CHAIN_ID}`"
)
if not RESOLVED_WALLET_ADDRESS:
    st.warning("WALLET_ADDRESS не определен: портфельные PnL/позиции будут недоступны, SELL-защита будет опираться только на allowance/balance CLOB.")


def push_log(log_queue: queue.Queue, text: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {text}"
    try:
        log_queue.put_nowait(line)
    except queue.Full:
        try:
            log_queue.get_nowait()
        except queue.Empty:
            pass
        try:
            log_queue.put_nowait(line)
        except queue.Full:
            pass


def push_metrics(metrics_queue: queue.Queue, payload: Dict[str, Any]) -> None:
    try:
        metrics_queue.put_nowait(payload)
    except queue.Full:
        try:
            metrics_queue.get_nowait()
        except queue.Empty:
            pass
        try:
            metrics_queue.put_nowait(payload)
        except queue.Full:
            pass


def safe_get_json(url: str, *, params: Optional[Dict[str, Any]] = None, timeout: int = 15, default: Any = None) -> Any:
    try:
        resp = http.get(url, params=params, timeout=timeout)
        if not resp.ok:
            return default
        return resp.json()
    except Exception:
        return default


def round_to_tick(price: float, tick: float, *, direction: str) -> float:
    if tick <= 0:
        return round(price, 4)

    steps = price / tick
    snapped = math.floor(steps) * tick if direction == "down" else math.ceil(steps) * tick
    precision = max(2, len(str(tick).split(".")[-1]) if "." in str(tick) else 2)
    return round(snapped, precision)


def parse_clob_token_ids(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(v) for v in value if str(v)]

    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return []
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return [str(v) for v in parsed if str(v)]
        except Exception:
            return []

    return []


def get_balance_allowance(asset_type: Any, token_id: Optional[str] = None) -> Optional[Dict[str, float]]:
    try:
        params = BalanceAllowanceParams(
            asset_type=asset_type,
            token_id=token_id,
            signature_type=-1,
        )
        response = client.get_balance_allowance(params)
        if not isinstance(response, dict):
            return None

        balance = to_float(response.get("balance"), -1.0)
        allowance = to_float(response.get("allowance"), -1.0)
        if balance < 0 or allowance < 0:
            return None

        return {
            "balance": balance,
            "allowance": allowance,
            "available": min(balance, allowance),
        }
    except Exception:
        return None


def fetch_positions_snapshot(wallet_address: Optional[str]) -> Dict[str, Any]:
    snapshot: Dict[str, Any] = {
        "positions_by_asset": {},
        "positions_count": 0,
        "initial_value": 0.0,
        "current_value": 0.0,
        "cash_pnl": 0.0,
        "realized_pnl": 0.0,
    }

    if not wallet_address:
        return snapshot

    positions = safe_get_json(
        f"{DATA_API_URL}/positions",
        params={"user": wallet_address},
        timeout=15,
        default=[],
    )

    if not isinstance(positions, list):
        return snapshot

    for pos in positions:
        asset = str(pos.get("asset") or "").strip()
        if not asset:
            continue

        size = max(0.0, to_float(pos.get("size"), 0.0))
        if size > 0:
            snapshot["positions_count"] += 1
            snapshot["positions_by_asset"][asset] = snapshot["positions_by_asset"].get(asset, 0.0) + size

        snapshot["initial_value"] += to_float(pos.get("initialValue"), 0.0)
        snapshot["current_value"] += to_float(pos.get("currentValue"), 0.0)
        snapshot["cash_pnl"] += to_float(pos.get("cashPnl"), 0.0)
        snapshot["realized_pnl"] += to_float(pos.get("realizedPnl"), 0.0)

    return snapshot


def bot_loop(
    order_size: float,
    spread_bps: int,
    refresh_sec: int,
    max_markets: int,
    max_cycle_notional: float,
    max_token_position: float,
    sell_inventory_buffer: float,
    max_portfolio_exposure: float,
    collateral_utilization: float,
    min_market_volume: float,
    enforce_sell_inventory: bool,
    stop_event: threading.Event,
    log_queue: queue.Queue,
    metrics_queue: queue.Queue,
    wallet_address: Optional[str],
) -> None:
    push_log(log_queue, "🚀 Бот запущен")

    while not stop_event.is_set():
        cycle_started = time.time()
        cycle_notional = 0.0
        buy_notional_reserved = 0.0

        orders_posted = 0
        buy_orders_posted = 0
        sell_orders_posted = 0

        skipped_no_inventory = 0
        skipped_balance = 0
        skipped_position_limit = 0
        skipped_exposure_limit = 0
        skipped_invalid_price = 0

        last_error = ""

        try:
            try:
                client.cancel_all()
                push_log(log_queue, "✅ Все ордера отменены")
            except Exception as exc:
                push_log(log_queue, f"⚠️ cancel_all не удался: {str(exc)[:120]}")

            collateral_info = get_balance_allowance(AssetType.COLLATERAL)
            collateral_available = collateral_info["available"] if collateral_info else 0.0

            positions_snapshot = fetch_positions_snapshot(wallet_address)
            positions_by_asset: Dict[str, float] = positions_snapshot["positions_by_asset"]
            portfolio_current_value = to_float(positions_snapshot.get("current_value"), 0.0)

            events = safe_get_json(
                f"{GAMMA_URL}/events",
                params={
                    "active": "true",
                    "closed": "false",
                    "order": "volume24hr",
                    "ascending": "false",
                    "limit": "80",
                },
                timeout=15,
                default=[],
            )

            if not isinstance(events, list):
                events = []

            markets = []
            for event in events:
                for market in event.get("markets", []):
                    token_ids = parse_clob_token_ids(market.get("clobTokenIds"))
                    if len(token_ids) != 2:
                        continue
                    if not to_bool(market.get("active"), True):
                        continue
                    if to_bool(market.get("closed"), False):
                        continue
                    if to_bool(market.get("archived"), False):
                        continue
                    if not to_bool(market.get("acceptingOrders"), False):
                        continue
                    if not to_bool(market.get("enableOrderBook"), True):
                        continue

                    volume = to_float(
                        market.get("volume24hr", market.get("volume_24hr", market.get("volume"))),
                        0.0,
                    )
                    if volume < min_market_volume:
                        continue

                    condition_id = market.get("conditionId")
                    if not condition_id:
                        continue

                    markets.append(
                        {
                            "condition_id": condition_id,
                            "token_ids": token_ids,
                            "slug": (market.get("slug") or "Unknown")[:40],
                            "volume": volume,
                        }
                    )

            markets = sorted(markets, key=lambda x: x["volume"], reverse=True)
            push_log(log_queue, f"📊 Кандидатов рынков: {len(markets)}")

            if not markets:
                push_log(log_queue, "⚠️ Нет подходящих рынков по фильтрам")
                push_metrics(
                    metrics_queue,
                    {
                        "last_cycle_ts": cycle_started,
                        "cycle_duration_sec": round(time.time() - cycle_started, 2),
                        "markets_seen": 0,
                        "orders_posted": 0,
                        "buy_orders_posted": 0,
                        "sell_orders_posted": 0,
                        "cycle_notional": 0.0,
                        "collateral_available": collateral_available,
                        **positions_snapshot,
                    },
                )
                stop_event.wait(refresh_sec)
                continue

            tradable_markets_count = 0
            skipped_no_orderbook = 0
            for market in markets:
                if stop_event.is_set():
                    break
                if tradable_markets_count >= max_markets:
                    break
                cycle_limit_hit = False

                try:
                    token_id = None
                    mid = -1.0
                    midpoint_errors = []
                    for candidate in market["token_ids"]:
                        try:
                            candidate_mid = to_float(client.get_midpoint(candidate), -1.0)
                            if 0.0 < candidate_mid < 1.0:
                                token_id = candidate
                                mid = candidate_mid
                                break
                            midpoint_errors.append(f"{candidate}: midpoint={candidate_mid}")
                        except Exception as exc:
                            midpoint_errors.append(f"{candidate}: {str(exc)[:80]}")

                    if token_id is None:
                        skipped_invalid_price += 1
                        skipped_no_orderbook += 1
                        if skipped_no_orderbook <= 2:
                            push_log(
                                log_queue,
                                f"⚠️ Пропуск {market['slug']}: нет активной книги ордеров "
                                f"({'; '.join(midpoint_errors)[:140]})",
                            )
                        continue

                    tradable_markets_count += 1

                    if not (0.0 < mid < 1.0):
                        skipped_invalid_price += 1
                        push_log(log_queue, f"⚠️ Пропуск {market['slug']}: midpoint вне диапазона ({mid})")
                        continue

                    info = safe_get_json(f"{GAMMA_URL}/markets/{market['condition_id']}", timeout=10, default={})
                    if not isinstance(info, dict):
                        info = {}

                    tick = max(0.001, to_float(info.get("minimumTickSize"), 0.01))
                    neg_risk = bool(info.get("negRisk", False))

                    spread = max(spread_bps / 10000.0, tick * 2)
                    raw_buy = max(0.01, mid - spread / 2)
                    raw_sell = min(0.99, mid + spread / 2)

                    p_buy = round_to_tick(raw_buy, tick, direction="down")
                    p_sell = round_to_tick(raw_sell, tick, direction="up")

                    if p_buy >= p_sell:
                        p_buy = max(0.01, round_to_tick(mid - tick, tick, direction="down"))
                        p_sell = min(0.99, round_to_tick(mid + tick, tick, direction="up"))

                    if p_buy >= p_sell:
                        skipped_invalid_price += 1
                        push_log(log_queue, f"⚠️ Пропуск {market['slug']}: невалидные цены")
                        continue

                    token_position = positions_by_asset.get(token_id, 0.0)

                    buy_size = float(order_size)
                    sell_size = float(order_size)

                    if token_position >= max_token_position:
                        buy_size = 0.0
                        skipped_position_limit += 1

                    if portfolio_current_value >= max_portfolio_exposure:
                        buy_size = 0.0
                        skipped_exposure_limit += 1

                    buy_required = p_buy * buy_size
                    max_buying_power = collateral_available * collateral_utilization
                    if buy_size > 0 and (buy_notional_reserved + buy_required > max_buying_power):
                        buy_size = 0.0
                        skipped_balance += 1

                    if enforce_sell_inventory:
                        inventory_caps = []
                        if wallet_address:
                            inventory_caps.append(max(0.0, token_position - sell_inventory_buffer))

                        conditional_info = get_balance_allowance(AssetType.CONDITIONAL, token_id=token_id)
                        if conditional_info:
                            inventory_caps.append(max(0.0, conditional_info["available"] - sell_inventory_buffer))

                        if inventory_caps:
                            sell_size = min(sell_size, min(inventory_caps))
                        else:
                            sell_size = 0.0

                        if sell_size <= 0.0:
                            skipped_no_inventory += 1

                    side_orders = []
                    if buy_size > 0:
                        side_orders.append((BUY, p_buy, buy_size))
                    if sell_size > 0:
                        side_orders.append((SELL, p_sell, sell_size))

                    if not side_orders:
                        continue

                    for side, price, size in side_orders:
                        order_notional = price * size
                        if cycle_notional + order_notional > max_cycle_notional:
                            push_log(log_queue, "🛑 Достигнут лимит notional на цикл")
                            cycle_limit_hit = True
                            break

                        order = OrderArgs(
                            token_id=token_id,
                            price=price,
                            size=size,
                            side=side,
                            order_type=OrderType.GTC,
                        )
                        client.create_and_post_order(
                            order,
                            {
                                "tick_size": str(tick),
                                "neg_risk": neg_risk,
                            },
                        )

                        cycle_notional += order_notional
                        orders_posted += 1
                        if side == BUY:
                            buy_orders_posted += 1
                            buy_notional_reserved += order_notional
                        else:
                            sell_orders_posted += 1

                        push_log(
                            log_queue,
                            f"✅ {'BUY' if side == BUY else 'SELL'} {market['slug']} size={size:.2f} @ {price}",
                        )

                except Exception as exc:
                    last_error = str(exc)[:160]
                    push_log(log_queue, f"⚠️ Ошибка рынка {market['slug']}: {last_error}")

                if cycle_limit_hit:
                    break

            if tradable_markets_count == 0:
                push_log(log_queue, "⚠️ Не найдено рынков с активной книгой ордеров в текущем цикле")
            else:
                push_log(log_queue, f"✅ Рынков с активной книгой в работе: {tradable_markets_count}")
            if skipped_no_orderbook > 2:
                push_log(log_queue, f"⚠️ Пропущено рынков без книги: {skipped_no_orderbook}")

            push_metrics(
                metrics_queue,
                {
                    "last_cycle_ts": cycle_started,
                    "cycle_duration_sec": round(time.time() - cycle_started, 2),
                    "markets_seen": len(markets),
                    "markets_tradable": tradable_markets_count,
                    "orders_posted": orders_posted,
                    "buy_orders_posted": buy_orders_posted,
                    "sell_orders_posted": sell_orders_posted,
                    "cycle_notional": round(cycle_notional, 2),
                    "collateral_available": round(collateral_available, 2),
                    "skipped_no_inventory": skipped_no_inventory,
                    "skipped_balance": skipped_balance,
                    "skipped_position_limit": skipped_position_limit,
                    "skipped_exposure_limit": skipped_exposure_limit,
                    "skipped_invalid_price": skipped_invalid_price,
                    "skipped_no_orderbook": skipped_no_orderbook,
                    "last_error": last_error,
                    **positions_snapshot,
                },
            )

            push_log(log_queue, f"⏳ Следующий цикл через {refresh_sec} сек...")
            stop_event.wait(refresh_sec)

        except Exception as exc:
            push_log(log_queue, f"❌ Критическая ошибка: {str(exc)[:160]}")
            push_metrics(
                metrics_queue,
                {
                    "last_cycle_ts": cycle_started,
                    "cycle_duration_sec": round(time.time() - cycle_started, 2),
                    "last_error": str(exc)[:160],
                },
            )
            stop_event.wait(5)

    try:
        client.cancel_all()
    except Exception:
        pass
    push_log(log_queue, "🛑 Бот остановлен")


thread = st.session_state.worker_thread
if st.session_state.running and (thread is None or not thread.is_alive()):
    st.session_state.running = False

with st.sidebar:
    st.header("Настройки")
    order_size = st.slider("Размер ордера (shares)", 1.0, 200.0, 10.0, 1.0)
    spread_bps = st.slider("Спред (bps)", 10, 80, 20, 1)
    refresh_sec = st.slider("Интервал цикла (сек)", 5, 90, 10, 1)
    max_markets = st.slider("Макс. рынков", 1, 12, 4, 1)

    st.divider()
    st.subheader("Risk")
    max_cycle_notional = st.slider("Лимит notional на цикл (USDC)", 20, 2000, 250, 10)
    max_portfolio_exposure = st.slider("Лимит экспозиции портфеля (USDC)", 50, 10000, 1000, 50)
    max_token_position = st.slider("Лимит позиции на токен (shares)", 1.0, 1000.0, 150.0, 1.0)
    sell_inventory_buffer = st.slider("Буфер для SELL (shares)", 0.0, 50.0, 1.0, 0.5)
    collateral_utilization = st.slider("Использование collateral", 0.1, 1.0, 0.85, 0.05)
    min_market_volume = st.slider("Мин. объем рынка 24h", 0, 500000, 10000, 1000)
    enforce_sell_inventory = st.checkbox("Запрет SELL без доступного остатка", value=ENABLE_SELL_INVENTORY_GUARD)

    col1, col2 = st.columns(2)
    if col1.button("▶ Запустить", type="primary", use_container_width=True):
        active_thread = st.session_state.worker_thread
        if active_thread is not None and active_thread.is_alive():
            push_log(st.session_state.log_queue, "ℹ️ Бот уже запущен")
        else:
            stop_event = threading.Event()
            st.session_state.stop_event = stop_event
            st.session_state.running = True

            worker = threading.Thread(
                target=bot_loop,
                args=(
                    float(order_size),
                    int(spread_bps),
                    int(refresh_sec),
                    int(max_markets),
                    float(max_cycle_notional),
                    float(max_token_position),
                    float(sell_inventory_buffer),
                    float(max_portfolio_exposure),
                    float(collateral_utilization),
                    float(min_market_volume),
                    bool(enforce_sell_inventory),
                    stop_event,
                    st.session_state.log_queue,
                    st.session_state.metrics_queue,
                    RESOLVED_WALLET_ADDRESS,
                ),
                daemon=True,
            )
            st.session_state.worker_thread = worker
            worker.start()

    if col2.button("⏹ Остановить", type="secondary", use_container_width=True):
        st.session_state.running = False
        st.session_state.stop_event.set()
        try:
            client.cancel_all()
        except Exception:
            pass


while not st.session_state.metrics_queue.empty():
    try:
        latest = st.session_state.metrics_queue.get_nowait()
        st.session_state.runtime_metrics.update(latest)
    except queue.Empty:
        break

metrics = st.session_state.runtime_metrics
status_text = "🟢 **БОТ РАБОТАЕТ**" if st.session_state.running else "🔴 Остановлен"
st.subheader(f"Статус: {status_text}")

last_cycle_ts = to_float(metrics.get("last_cycle_ts"), 0.0)
seconds_since_cycle = int(time.time() - last_cycle_ts) if last_cycle_ts > 0 else None
health_ok = st.session_state.running and seconds_since_cycle is not None and seconds_since_cycle <= max(20, refresh_sec * 3)
health_state = "OK" if health_ok else ("IDLE" if not st.session_state.running else "STALE")

c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Health", health_state)
c2.metric("Collateral avail", f"{to_float(metrics.get('collateral_available'), 0.0):.2f}")
c3.metric("Portfolio value", f"{to_float(metrics.get('current_value'), 0.0):.2f}")
c4.metric("Cash PnL", f"{to_float(metrics.get('cash_pnl'), 0.0):.2f}")
c5.metric("Realized PnL", f"{to_float(metrics.get('realized_pnl'), 0.0):.2f}")

c6, c7, c8, c9, c10 = st.columns(5)
c6.metric("Orders/cycle", str(int(to_float(metrics.get("orders_posted"), 0))))
c7.metric("BUY/Sell", f"{int(to_float(metrics.get('buy_orders_posted'), 0))}/{int(to_float(metrics.get('sell_orders_posted'), 0))}")
c8.metric("Cycle notional", f"{to_float(metrics.get('cycle_notional'), 0.0):.2f}")
c9.metric("Open positions", str(int(to_float(metrics.get("positions_count"), 0))))
c10.metric("Cycle age (sec)", "-" if seconds_since_cycle is None else str(seconds_since_cycle))

st.caption(
    "Skip stats: "
    f"no_orderbook={int(to_float(metrics.get('skipped_no_orderbook'), 0))}, "
    f"no_inventory={int(to_float(metrics.get('skipped_no_inventory'), 0))}, "
    f"balance={int(to_float(metrics.get('skipped_balance'), 0))}, "
    f"position_limit={int(to_float(metrics.get('skipped_position_limit'), 0))}, "
    f"exposure_limit={int(to_float(metrics.get('skipped_exposure_limit'), 0))}, "
    f"invalid_price={int(to_float(metrics.get('skipped_invalid_price'), 0))}"
)

last_error = str(metrics.get("last_error") or "").strip()
if last_error:
    st.warning(f"Последняя ошибка: {last_error}")

while not st.session_state.log_queue.empty():
    try:
        st.session_state.log_buffer.append(st.session_state.log_queue.get_nowait())
    except queue.Empty:
        break

st.code("\n".join(st.session_state.log_buffer), language=None)

if st.session_state.running:
    time.sleep(1)
    st.rerun()
