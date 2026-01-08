import os
import re
import time
import unicodedata
import MetaTrader5 as mt5
from dotenv import load_dotenv
from telethon import TelegramClient, events

# ───────────────────────────────
# Config
# ───────────────────────────────
load_dotenv()

API_HASH = os.getenv("API_HASH")
API_ID = int(os.getenv("API_ID"))
CHAT_ID = os.getenv("CHAT_ID")
CONNECT_MT5 = os.getenv("CONNECT_MT5") == "True"
LIMIT_BUFFER = float(os.getenv("LIMIT_BUFFER"))
LIMIT_ONLY = os.getenv("LIMIT_ONLY") == "True"
MAGIC = int(os.getenv("MAGIC"))
RISK_PERCENT = float(os.getenv("RISK_PERCENT"))
SESSION_FILE = os.getenv("SESSION_FILE")
SYMBOL = os.getenv("SYMBOL")

client = TelegramClient(SESSION_FILE, API_ID, API_HASH)
chats = int(CHAT_ID) if CHAT_ID.lstrip("-").isdigit() else CHAT_ID

# ───────────────────────────────
# Telegram handler
# ───────────────────────────────
@client.on(events.NewMessage(chats=chats))
async def handler(event):
    signal_time_local = event.date.astimezone()

    print("──────────────────────────────────────")
    print(f"Señal #{event.id} @ {signal_time_local}")

    text = normalize_text(event.raw_text)

    # Mensajes de management
    if event.is_reply and ("delete" in text or "cancel" in text):
        replied = await event.get_reply_message()
        print(f"> Delete para señal #{replied.id}")
        if CONNECT_MT5:
            delete_pending_by_signal_id(replied.id)
        return
    
    if "sl move" in text and event.is_reply:
        replied = await event.get_reply_message()
        print(f"> Break-even para señal #{replied.id}")
        if CONNECT_MT5:
            move_sl_to_be_by_signal_id(replied.id)
        return
    
    if "close" in text and event.is_reply:
        replied = await event.get_reply_message()
        print(f"> Close para señal #{replied.id}")
        if CONNECT_MT5:
            close_position_by_signal_id(replied.id)
            # A veces close se refiere a órdenes pendientes
            delete_pending_by_signal_id(replied.id)
        return

    # Señales normales
    parsed = parse_signal(text)

    required = ["side", "entry", "sl", "tp"]
    if any(parsed[k] is None for k in required):
        print("> Mensaje no válido.")
        return

    print(">", parsed)

    if CONNECT_MT5:
        send_order(parsed, signal_id=event.id)

# ───────────────────────────────
# Parsing
# ───────────────────────────────
def normalize_text(text):
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    return text.lower()

def parse_signal(message: str):
    text = message.lower()

    side = "BUY" if " buy" in text else "SELL" if " sell" in text else None
    order_type = "LIMIT" if "limit" in text else "MARKET"

    entry_match = re.search(r"(?:@)?\s*(\d+(?:\.\d+)?)", text)
    sl_match = re.search(r"sl\s*@?\s*(\d+(?:\.\d+)?)", text)
    tp_match = re.search(r"tp\s*@?\s*(\d+(?:\.\d+)?)", text)

    return {
        "symbol": SYMBOL,
        "side": side,
        "order_type": order_type,
        "entry": float(entry_match.group(1)) if entry_match else None,
        "sl": float(sl_match.group(1)) if sl_match else None,
        "tp": float(tp_match.group(1)) if tp_match else None,
    }

# ───────────────────────────────
# Envío de órdenes
# ───────────────────────────────
def send_order(parsed, signal_id):
    symbol = parsed["symbol"]
    side = parsed["side"]
    sl = parsed["sl"]
    tp = parsed["tp"]
    entry = parsed["entry"]
    kind = parsed["order_type"]

    mt5.symbol_select(symbol, True)
    tick = mt5.symbol_info_tick(symbol)

    if LIMIT_ONLY or kind == "LIMIT":
        action = mt5.TRADE_ACTION_PENDING
        if side == "BUY":
            price = entry + LIMIT_BUFFER
            order_type = mt5.ORDER_TYPE_BUY_LIMIT
        else:
            order_type = mt5.ORDER_TYPE_SELL_LIMIT
            price = entry - LIMIT_BUFFER
    else:
        action = mt5.TRADE_ACTION_DEAL
        order_type = mt5.ORDER_TYPE_BUY if side == "BUY" else mt5.ORDER_TYPE_SELL
        price = tick.ask if side == "BUY" else tick.bid

    lot = calculate_lot(symbol, price, sl, RISK_PERCENT)
    if lot <= 0:
        print("Lote inválido.")
        return
    
    expiration_time = int(time.time()) + 3600 # 1 hora

    request = {
        "action": action,
        "symbol": symbol,
        "volume": lot,
        "type": order_type,
        "price": price,
        "sl": sl,
        "tp": tp,
        "deviation": 10,
        "magic": MAGIC,
        "comment": f"signal:{signal_id}",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
        "expiration": expiration_time,
    }

    result = mt5.order_send(request)
    print("Resultado:", result)

# ───────────────────────────────
# Delete pending por signal_id
# ───────────────────────────────
def delete_pending_by_signal_id(signal_id):
    orders = mt5.orders_get()
    if not orders:
        print("No hay órdenes pendientes.")
        return

    for o in orders:
        if o.magic != MAGIC:
            continue
        if o.comment != f"signal:{signal_id}":
            continue

        request = {
            "action": mt5.TRADE_ACTION_REMOVE,
            "order": o.ticket,
            "symbol": o.symbol,
            "magic": MAGIC,
            "comment": "delete_by_reply"
        }

        result = mt5.order_send(request)
        if result.retcode == mt5.TRADE_RETCODE_DONE:
            print(f"✅ Pending eliminada | ticket {o.ticket}")
        else:
            print(f"❌ Error eliminando {o.ticket}", result)

# ───────────────────────────────
# Close pending por signal_id
# ───────────────────────────────
def close_position_by_signal_id(signal_id):
    positions = mt5.positions_get()
    if not positions:
        print("No hay posiciones abiertas.")
        return

    for p in positions:
        if p.magic != MAGIC:
            continue
        if p.comment != f"signal:{signal_id}":
            continue

        close_type = (
            mt5.ORDER_TYPE_SELL
            if p.type == mt5.POSITION_TYPE_BUY
            else mt5.ORDER_TYPE_BUY
        )

        tick = mt5.symbol_info_tick(p.symbol)
        price = tick.bid if close_type == mt5.ORDER_TYPE_SELL else tick.ask

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": p.symbol,
            "volume": p.volume,
            "type": close_type,
            "position": p.ticket,
            "price": price,
            "deviation": 10,
            "magic": MAGIC,
            "comment": "close_by_reply",
            "type_filling": mt5.ORDER_FILLING_IOC,
        }

        result = mt5.order_send(request)
        if result.retcode == mt5.TRADE_RETCODE_DONE:
            print(f"✅ Posición cerrada | ticket {p.ticket}")
        else:
            print(f"❌ Error cerrando {p.ticket}", result)
            # Medida preventiva por si reducir el SL falla
            close_position_by_signal_id(signal_id)

# ───────────────────────────────
# Mover SL a un factor de riesgo
# ───────────────────────────────
def reduce_sl_by_factor_by_signal_id(signal_id, factor):
    positions = mt5.positions_get()
    if not positions:
        print("No hay posiciones abiertas.")
        return

    for p in positions:
        if p.magic != MAGIC:
            continue
        if p.comment != f"signal:{signal_id}":
            continue

        entry = p.price_open
        sl = p.sl

        # BUY
        if p.type == mt5.POSITION_TYPE_BUY:
            new_sl = entry + (sl - entry) * factor

        # SELL
        else:
            new_sl = entry - (entry - sl) * factor

        request = {
            "action": mt5.TRADE_ACTION_SLTP,
            "position": p.ticket,
            "symbol": p.symbol,
            "sl": new_sl,
            "tp": p.tp,
            "magic": MAGIC,
            "comment": f"reduce_sl_{factor}"
        }

        result = mt5.order_send(request)
        if result.retcode == mt5.TRADE_RETCODE_DONE:
            print(f"✅ SL movido a la mitad | ticket {p.ticket}")
        else:
            print(f"❌ Error moviendo SL {p.ticket}", result)

# ───────────────────────────────
# Mover SL to BE por signal_id
# ───────────────────────────────
def move_sl_to_be_by_signal_id(signal_id):
    positions = mt5.positions_get()
    if not positions:
        print("No hay posiciones abiertas.")
        return

    for p in positions:
        if p.magic != MAGIC:
            continue
        if p.comment != f"signal:{signal_id}":
            continue

        entry = p.price_open

        request = {
            "action": mt5.TRADE_ACTION_SLTP,
            "position": p.ticket,
            "symbol": p.symbol,
            "sl": entry,
            "tp": p.tp,
            "magic": MAGIC,
            "comment": "move_sl_to_be"
        }

        result = mt5.order_send(request)
        if result.retcode == mt5.TRADE_RETCODE_DONE:
            print(f"✅ SL movido a BE | ticket {p.ticket}")
        else:
            print(f"❌ Error moviendo SL {p.ticket}", result)
            # Medida preventiva por si el BE falla
            reduce_sl_by_factor_by_signal_id(signal_id, 0.2)

# ───────────────────────────────
# Calculo de Lotaje
# ───────────────────────────────
def calculate_lot(symbol, entry_price, sl_price, risk_percent):
    acc = mt5.account_info()
    sym = mt5.symbol_info(symbol)

    risk_money = acc.balance * (risk_percent / 100.0)
    stop_diff = abs(entry_price - sl_price)

    value_per_price = sym.trade_tick_value / sym.trade_tick_size
    units = risk_money / (stop_diff * (value_per_price / sym.trade_contract_size))
    lots = units / sym.trade_contract_size

    lots = round(lots / sym.volume_step) * sym.volume_step
    if lots < sym.volume_min:
        return 0.0
    return min(lots, sym.volume_max)

# ───────────────────────────────
# Init
# ───────────────────────────────
def init_mt5():
    if not mt5.initialize():
        raise RuntimeError(mt5.last_error())
    print("MT5 conectado.")

async def main():
    await client.start()
    print("Bot escuchando...")
    await client.run_until_disconnected()

with client:
    if CONNECT_MT5:
        init_mt5()
    client.loop.run_until_complete(main())
