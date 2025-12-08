import os
import re
import MetaTrader5 as mt5
from dotenv import load_dotenv
from telethon import TelegramClient, events

# Cargar .env
load_dotenv()

api_id = int(os.getenv("API_ID"))
api_hash = os.getenv("API_HASH")
session = os.getenv("SESSION_FILE")

client = TelegramClient(session, api_id, api_hash)

@client.on(events.NewMessage())
async def handler(event):
    chat_id = event.chat_id
    chat = await event.get_chat()

    # Algunos chats tienen title, otros first_name
    chat_name = getattr(chat, "title", None) or getattr(chat, "first_name", "Desconocido")

    print("────────────")
    print("Chat ID:", chat_id)
    print("Nombre  :", chat_name)
    # print("Mensaje :", event.raw_text)

    parsed = parse_signal(event.raw_text)
    print("Señal recibida:", parsed)

    # Si falta BUY/SELL o SL/TP, ignora el mensaje
    if not parsed["side"] or not parsed["sl"] or not parsed["tp"]:
        print("Mensaje no válido.")
        return

    # ENVIAR LA ORDEN A MT5
    send_order(parsed)

def parse_signal(message: str):
    text = message.upper()

    # Detectar BUY o SELL
    side_match = re.search(r"\b(BUY|SELL)\b", text)
    side = side_match.group(1) if side_match else None

    # Detectar SL
    sl_match = re.search(r"\bSL\s+(\d+(\.\d+)?)", text)
    sl = float(sl_match.group(1)) if sl_match else None

    # Detectar la lista de TP (pueden ser varios)
    tp_matches = re.findall(r"\bTP\d*\s+(\d+(?:\.\d+)?)", text)
    tp = float(tp_matches[1]) if tp_matches else None

    return {
        "symbol": "XAUUSD",   # siempre es XAU en tus señales
        "side": side,         # BUY / SELL
        "tp": tp,            # segundo TP
        "sl": sl              # stop loss
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

    lot = calculate_lot(symbol, price, sl, risk_percent=1.0)

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
        "magic": 55555,
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

    # === Cálculo tipo "por unidad" (equivalente a Symbol.PipValue de cTrader) ===
    # trade_tick_value = valor monetario de UN TICK para 1 LOTE
    # trade_tick_size  = tamaño de ese tick en precio (ej: 0.01)
    # contract_size    = unidades por lote (ej: 1 para BTC, 100000 para EURUSD)
    value_per_price_unit_per_lot = trade_tick_value / trade_tick_size
    value_per_price_unit_per_unit = value_per_price_unit_per_lot / contract_size

    # Ahora replicamos rawUnits = riskMoney / (stopPips * PipValue)
    # pero usando stop_price_diff en **unidades de precio**:
    raw_units = risk_money / (stop_price_diff * value_per_price_unit_per_unit)

    # Convertir unidades -> lotes
    lots_raw = raw_units / contract_size

    # Normalizar al paso de volumen del broker
    # Evitar problemas de punto flotante usando round con el step
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
    init_mt5()
    client.loop.run_until_complete(main())
