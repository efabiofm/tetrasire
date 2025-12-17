import os
import re
import unicodedata
import MetaTrader5 as mt5
from dotenv import load_dotenv
from telethon import TelegramClient, events

# Cargar .env
load_dotenv()

SYMBOL = os.getenv("SYMBOL")
MAGIC = int(os.getenv("MAGIC"))
SESSION_FILE = os.getenv("SESSION_FILE")
API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
CONNECT_MT5 = os.getenv("CONNECT_MT5") == "True"
TP_TARGET = int(os.getenv("TP_TARGET"))
BE_AT_TP = int(os.getenv("BE_AT_TP"))
RISK_PERCENT = float(os.getenv("RISK_PERCENT"))

client = TelegramClient(SESSION_FILE, API_ID, API_HASH)

@client.on(events.NewMessage())
async def handler(event):
    chat_id = event.chat_id
    chat = await event.get_chat()
    text = normalize_text(event.raw_text)

    # Algunos chats tienen title, otros first_name
    chat_name = getattr(chat, "title", None) or getattr(chat, "first_name", "Desconocido")

    print("────────────")
    print("Chat ID:", chat_id)
    print("Nombre  :", chat_name)

    # --- CIERRE DE POSICIÓN ---
    if "CLOSE" in text.upper():
        print("📩 Señal CLOSE detectada")
        close_bot_positions()
        return
    
    # --- MOVER SL A BE ---
    if BE_AT_TP > 0 and f"TP {BE_AT_TP} HIT" in text.upper():
        print("Señal BE detectada")
        move_bot_positions_to_be()
        return

    parsed = parse_signal(text)
    print("Señal recibida:", parsed)

    # Si falta BUY/SELL o SL/TP, ignora el mensaje
    if not parsed["side"] or not parsed["sl"] or not parsed["tp"]:
        print("Mensaje no válido.")
        return

    # ENVIAR LA ORDEN A MT5
    if CONNECT_MT5:
        # Cerrar cualquier posición que podría estar en perdida
        close_bot_positions()
        send_order(parsed)

def normalize_text(text):
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")

def parse_signal(message: str):
    text = message.upper()

    # Detectar BUY o SELL
    side_match = re.search(r"\b(BUY|SELL)\b", text)
    side = side_match.group(1) if side_match else None

    # Detectar SL
    sl_match = re.search(r"\bSL\b.*?(\d+(?:\.\d+)?)", text)
    sl = float(sl_match.group(1)) if sl_match else None

    # Detectar la lista de TP (pueden ser varios)
    tp_matches = re.findall(r"\bTP\d*\s+(\d+(?:\.\d+)?)", text)
    tp = float(tp_matches[TP_TARGET - 1]) if len(tp_matches) > 1 else None

    return {
        "symbol": SYMBOL,
        "side": side,
        "tp": tp,
        "sl": sl
    }

def send_order(parsed):
    symbol = parsed["symbol"]
    side = parsed["side"]
    sl = parsed["sl"]
    tp = parsed["tp"]

    # BUY = ORDER_TYPE_BUY, SELL = ORDER_TYPE_SELL
    order_type = mt5.ORDER_TYPE_BUY if side == "BUY" else mt5.ORDER_TYPE_SELL

    # Precios actuales del símbolo
    symbol_info = mt5.symbol_info(symbol)
    if symbol_info is None:
        raise RuntimeError(f"Símbolo no encontrado: {symbol}")

    if not symbol_info.visible:
        mt5.symbol_select(symbol, True)

    price = mt5.symbol_info_tick(symbol).ask if side == "BUY" else mt5.symbol_info_tick(symbol).bid

    lot = calculate_lot(symbol, price, sl, risk_percent=RISK_PERCENT)

    # Crear orden
    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": lot,
        "type": order_type,
        "price": price,
        "sl": sl,
        "tp": tp,
        "deviation": 10,
        "magic": MAGIC,
        "comment": "telegram_signal",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }

    # Enviar orden
    result = mt5.order_send(request)

    if result.retcode != mt5.TRADE_RETCODE_DONE:
        print("Error al enviar orden:", result)
    else:
        print("Orden enviada:", result)

def move_bot_positions_to_be():
    positions = mt5.positions_get()
    if not positions:
        return

    for p in positions:
        if p.magic != MAGIC:
            continue

        entry = p.price_open

        request = {
            "action": mt5.TRADE_ACTION_SLTP,
            "position": p.ticket,
            "sl": entry,
            "tp": p.tp,
            "symbol": p.symbol,
            "magic": MAGIC,
            "comment": "move_to_be"
        }

        mt5.order_send(request)

def close_bot_positions():
    positions = mt5.positions_get()
    if positions is None:
        print("No se pudieron obtener posiciones.")
        return

    for p in positions:
        if p.magic != MAGIC or p.symbol != SYMBOL:
            continue

        close_type = mt5.ORDER_TYPE_SELL if p.type == mt5.POSITION_TYPE_BUY else mt5.ORDER_TYPE_BUY
        price = mt5.symbol_info_tick(p.symbol).bid if close_type == mt5.ORDER_TYPE_SELL else mt5.symbol_info_tick(p.symbol).ask

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": p.symbol,
            "volume": p.volume,
            "type": close_type,
            "position": p.ticket,
            "price": price,
            "deviation": 10,
            "magic": MAGIC,
            "comment": "telegram_close"
        }

        result = mt5.order_send(request)

        if result.retcode == mt5.TRADE_RETCODE_DONE:
            print(f"✅ Posición cerrada | ticket {p.ticket}")
        else:
            print(f"❌ Error cerrando ticket {p.ticket}", result)

def calculate_lot(symbol, entry_price, sl_price, risk_percent=1.0, debug=False):
    acc = mt5.account_info()
    if acc is None:
        raise RuntimeError("No hay conexión a la cuenta (account_info() devolvió None).")

    balance = acc.balance
    risk_money = balance * (risk_percent / 100.0)

    sym = mt5.symbol_info(symbol)
    if sym is None:
        raise RuntimeError(f"Símbolo {symbol} no encontrado.")

    # propiedades del símbolo
    trade_tick_value = getattr(sym, "trade_tick_value", None)
    trade_tick_size = getattr(sym, "trade_tick_size", None)
    contract_size = getattr(sym, "trade_contract_size", None)  # unidades por lote
    point = getattr(sym, "point", None)

    volume_step = getattr(sym, "volume_step", None)
    volume_min = getattr(sym, "volume_min", None)
    volume_max = getattr(sym, "volume_max", None)

    if None in (trade_tick_value, trade_tick_size, contract_size, point, volume_step, volume_min, volume_max):
        raise RuntimeError("El símbolo no provee todos los parámetros necesarios.")

    # Distancia SL en precio (misma unidad que entry_price/sl_price)
    stop_price_diff = abs(entry_price - sl_price)
    if stop_price_diff <= 0:
        raise ValueError("SL inválido o igual al precio de entrada.")

    value_per_price_unit_per_lot = trade_tick_value / trade_tick_size
    value_per_price_unit_per_unit = value_per_price_unit_per_lot / contract_size
    raw_units = risk_money / (stop_price_diff * value_per_price_unit_per_unit)
    lots_raw = raw_units / contract_size
    normalized_lots = round(lots_raw / volume_step) * volume_step

    # Respetar min/max
    if normalized_lots < volume_min:
        normalized_lots = 0.0  # opcional: devolver 0 si menor que min
    if normalized_lots > volume_max:
        normalized_lots = volume_max

    return normalized_lots

def init_mt5():
    if not mt5.initialize():
        raise RuntimeError(f"MT5 no pudo inicializarse: {mt5.last_error()}")
    print("MT5 conectado.")

async def main():
    await client.start()
    print("Bot conectado y escuchando mensajes...")
    await client.run_until_disconnected()

with client:
    if CONNECT_MT5:
        init_mt5()
    client.loop.run_until_complete(main())
