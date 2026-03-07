import os
import time
import requests
import threading
from simmer_sdk import SimmerClient

# === CONFIG ===
SIMMER_API_KEY = os.environ.get("SIMMER_API_KEY")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
STAKE = float(os.environ.get("MAX_USD", "1"))

raw_wallets = os.environ.get("SIMMER_COPYTRADING_WALLETS", "")
TRADERS = [w.strip() for w in raw_wallets.split(",") if w.strip()]

# Umbral mínimo de edge para apostar con bot propio (ej: 0.08 = 8%)
EDGE_THRESHOLD = 0.08

# Rango de precio válido para copy trading
COPY_MIN_PRICE = 0.20
COPY_MAX_PRICE = 0.75

# === TELEGRAM ===
def notify(msg):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"[NOTIFY] {msg}")
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": msg}, timeout=10)
    except Exception as e:
        print(f"⚠️ Telegram error: {e}")

# === SIMMER CLIENT ===
client = SimmerClient(api_key=SIMMER_API_KEY, venue="polymarket")

# === LOCK para evitar trades simultáneos ===
trade_lock = threading.Lock()
trades_copiados = set()

def ejecutar_trade(market_id, side, razon, precio_ref=None):
    with trade_lock:
        try:
            result = client.trade(
                market_id=market_id,
                side=side,
                amount=STAKE
            )
            msg = (
                f"✅ Trade ejecutado!\n"
                f"Razón: {razon}\n"
                f"Side: {side.upper()}\n"
                f"Precio ref: {precio_ref}\n"
                f"Stake: ${STAKE}\n"
                f"Resultado: {result}"
            )
            print(msg)
            notify(msg)
            return True
        except Exception as e:
            err = f"❌ Error ejecutando trade: {e}\nMercado: {market_id[:30]}..."
            print(err)
            notify(err)
            return False

# ============================================================
# MOTOR 1: COPY TRADING
# ============================================================
def get_trades_del_trader(wallet):
    url = f"https://data-api.polymarket.com/activity?user={wallet}&limit=5&type=TRADE"
    try:
        r = requests.get(url, timeout=10)
        return r.json()
    except Exception as e:
        print(f"❌ Error obteniendo trades de {wallet[:8]}: {e}")
        return []

def motor_copy_trading():
    print("🔁 [COPY] Motor de copy trading iniciado")
    while True:
        try:
            for wallet in TRADERS:
                trades = get_trades_del_trader(wallet)
                for trade in trades:
                    trade_id = trade.get("id")
                    if not trade_id or trade_id in trades_copiados:
                        continue

                    asset = trade.get("asset") or trade.get("conditionId")
                    side = trade.get("side", "").upper()
                    try:
                        price = float(trade.get("price", 0))
                    except:
                        continue

                    if not asset or not side or price <= 0:
                        continue

                    # Marcar como visto siempre (para no reprocesar)
                    trades_copiados.add(trade_id)

                    # Filtro de precio — solo copiamos si hay margen
                    if side not in ("BUY", "YES"):
                        continue
                    if not (COPY_MIN_PRICE <= price <= COPY_MAX_PRICE):
                        print(f"⏭️ [COPY] Precio {price} fuera de rango, skip")
                        continue

                    print(f"🔔 [COPY] Trade detectado de {wallet[:8]}... | Price: {price}")
                    ejecutar_trade(
                        market_id=asset,
                        side="yes",
                        razon=f"Copy de {wallet[:8]} @ {price}",
                        precio_ref=price
                    )

            time.sleep(30)
        except Exception as e:
            print(f"❌ [COPY] Error general: {e}")
            time.sleep(30)

# ============================================================
# MOTOR 2: BOT CLIMÁTICO (Open-Meteo + Polymarket)
# ============================================================

# Ciudades con coordenadas para consultar Open-Meteo
CIUDADES = {
    "New York":     {"lat": 40.71, "lon": -74.01},
    "Los Angeles":  {"lat": 34.05, "lon": -118.24},
    "Miami":        {"lat": 25.77, "lon": -80.19},
    "Chicago":      {"lat": 41.88, "lon": -87.63},
    "London":       {"lat": 51.51, "lon": -0.13},
}

def get_precipitacion_prob(ciudad):
    """Obtiene probabilidad de precipitación de Open-Meteo (gratuito, sin API key)"""
    coords = CIUDADES.get(ciudad)
    if not coords:
        return None
    url = (
        f"https://api.open-meteo.com/v1/forecast"
        f"?latitude={coords['lat']}&longitude={coords['lon']}"
        f"&daily=precipitation_probability_max"
        f"&forecast_days=1&timezone=auto"
    )
    try:
        r = requests.get(url, timeout=10)
        data = r.json()
        prob = data["daily"]["precipitation_probability_max"][0]
        return prob / 100.0  # convertir a 0-1
    except Exception as e:
        print(f"⚠️ [CLIMA] Error Open-Meteo para {ciudad}: {e}")
        return None

def get_mercados_clima():
    """Busca mercados activos de clima en Polymarket"""
    keywords = ["rain", "rainfall", "precipitation", "storm", "snow", "temperature", "weather"]
    mercados = []
    for keyword in keywords[:3]:  # limitamos para no spamear
        try:
            url = f"https://gamma-api.polymarket.com/markets?active=true&closed=false&limit=20&search={keyword}"
            r = requests.get(url, timeout=10)
            data = r.json()
            if isinstance(data, list):
                mercados.extend(data)
            time.sleep(1)
        except Exception as e:
            print(f"⚠️ [CLIMA] Error buscando mercados '{keyword}': {e}")
    return mercados

def analizar_mercado_clima(mercado):
    """
    Analiza un mercado de clima y decide si hay edge.
    Retorna (side, edge) o None si no hay oportunidad.
    """
    pregunta = mercado.get("question", "").lower()
    precio_yes = None

    # Intentar obtener precio YES
    try:
        tokens = mercado.get("tokens", [])
        for token in tokens:
            if token.get("outcome", "").upper() == "YES":
                precio_yes = float(token.get("price", 0))
                break
        if precio_yes is None:
            precio_yes = float(mercado.get("bestAsk", 0) or mercado.get("lastTradePrice", 0))
    except:
        return None

    if not precio_yes or precio_yes <= 0:
        return None

    # Detectar ciudad mencionada en la pregunta
    ciudad_detectada = None
    for ciudad in CIUDADES.keys():
        if ciudad.lower() in pregunta:
            ciudad_detectada = ciudad
            break

    if not ciudad_detectada:
        return None

    # Obtener probabilidad real de Open-Meteo
    prob_real = get_precipitacion_prob(ciudad_detectada)
    if prob_real is None:
        return None

    # Calcular edge
    edge_yes = prob_real - precio_yes
    edge_no  = (1 - prob_real) - (1 - precio_yes)

    print(f"📊 [CLIMA] {ciudad_detectada} | Mercado: {precio_yes:.2f} | Open-Meteo: {prob_real:.2f} | Edge YES: {edge_yes:.2f}")

    if edge_yes >= EDGE_THRESHOLD:
        return ("yes", edge_yes)
    elif edge_no >= EDGE_THRESHOLD:
        return ("no", edge_no)

    return None

mercados_apostados = set()

def motor_climatico():
    print("🌦️ [CLIMA] Motor climático iniciado")
    while True:
        try:
            mercados = get_mercados_clima()
            print(f"🌦️ [CLIMA] {len(mercados)} mercados de clima encontrados")

            for mercado in mercados:
                market_id = mercado.get("conditionId") or mercado.get("id")
                if not market_id or market_id in mercados_apostados:
                    continue

                resultado = analizar_mercado_clima(mercado)
                if resultado:
                    side, edge = resultado
                    pregunta = mercado.get("question", "")[:60]
                    print(f"🎯 [CLIMA] Edge encontrado! {side.upper()} | Edge: {edge:.2f} | {pregunta}")

                    ok = ejecutar_trade(
                        market_id=market_id,
                        side=side,
                        razon=f"Bot climático | Edge: {edge:.2f}",
                        precio_ref=edge
                    )
                    if ok:
                        mercados_apostados.add(market_id)

            # Revisar mercados de clima cada 10 minutos
            time.sleep(600)

        except Exception as e:
            print(f"❌ [CLIMA] Error general: {e}")
            time.sleep(60)

# ============================================================
# MAIN
# ============================================================
print("🤖 Bot iniciado con 2 motores en paralelo")
print(f"👀 Copy trading: {len(TRADERS)} traders | Rango precio: {COPY_MIN_PRICE}-{COPY_MAX_PRICE}")
print(f"🌦️ Bot climático: Open-Meteo | Edge mínimo: {EDGE_THRESHOLD*100:.0f}%")
print(f"💰 Stake fijo: ${STAKE}")
notify(f"🤖 Bot iniciado!\n💰 Stake: ${STAKE}\n👀 Copiando {len(TRADERS)} traders\n🌦️ Bot climático activo")

t1 = threading.Thread(target=motor_copy_trading, daemon=True)
t2 = threading.Thread(target=motor_climatico, daemon=True)

t1.start()
t2.start()

# Mantener el proceso principal vivo
while True:
    time.sleep(60)
