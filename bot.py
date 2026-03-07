import os
import time
import requests
import threading
import math
from simmer_sdk import SimmerClient

# === CONFIG ===
SIMMER_API_KEY = os.environ.get("SIMMER_API_KEY")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
STAKE = float(os.environ.get("MAX_USD", "1"))

raw_wallets = os.environ.get("SIMMER_COPYTRADING_WALLETS", "")
TRADERS = [w.strip() for w in raw_wallets.split(",") if w.strip()]

EDGE_THRESHOLD = float(os.environ.get("EDGE_THRESHOLD", "0.05"))
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

def importar_mercado(condition_id):
    """Importa un mercado de Polymarket a Simmer si no existe todavía"""
    try:
        url = f"https://polymarket.com/event/{condition_id}"
        result = client.import_market(url)
        simmer_id = result.get("id") if isinstance(result, dict) else None
        if simmer_id:
            print(f"✅ Mercado importado a Simmer: {simmer_id}")
            return simmer_id
    except Exception as e:
        print(f"⚠️ Error importando mercado: {e}")
    # Intentar con slug directo
    try:
        result = client.import_market(f"https://polymarket.com/market/{condition_id}")
        simmer_id = result.get("id") if isinstance(result, dict) else None
        return simmer_id
    except:
        pass
    return None

def ejecutar_trade(market_id, side, razon, precio_ref=None, slug=None):
    with trade_lock:
        try:
            # Intentar importar el mercado a Simmer primero
            simmer_id = importar_mercado(market_id)
            trade_id = simmer_id or market_id

            result = client.trade(
                market_id=trade_id,
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
            err = f"❌ Error ejecutando trade: {e}\nMercado: {str(market_id)[:40]}..."
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

                    trades_copiados.add(trade_id)

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
# MOTOR 2: BOT CLIMÁTICO
# ============================================================
CIUDADES = {
    # USA
    "New York":      {"lat": 40.71,  "lon": -74.01},
    "Los Angeles":   {"lat": 34.05,  "lon": -118.24},
    "Miami":         {"lat": 25.77,  "lon": -80.19},
    "Chicago":       {"lat": 41.88,  "lon": -87.63},
    "Houston":       {"lat": 29.76,  "lon": -95.37},
    "Phoenix":       {"lat": 33.45,  "lon": -112.07},
    "Seattle":       {"lat": 47.61,  "lon": -122.33},
    # Europa
    "London":        {"lat": 51.51,  "lon": -0.13},
    "Paris":         {"lat": 48.85,  "lon": 2.35},
    "Berlin":        {"lat": 52.52,  "lon": 13.40},
    "Madrid":        {"lat": 40.42,  "lon": -3.70},
    "Rome":          {"lat": 41.90,  "lon": 12.50},
    "Amsterdam":     {"lat": 52.37,  "lon": 4.90},
    "Vienna":        {"lat": 48.21,  "lon": 16.37},
    # Asia
    "Tokyo":         {"lat": 35.69,  "lon": 139.69},
    "Beijing":       {"lat": 39.90,  "lon": 116.41},
    "Shanghai":      {"lat": 31.23,  "lon": 121.47},
    "Mumbai":        {"lat": 19.08,  "lon": 72.88},
    "Delhi":         {"lat": 28.61,  "lon": 77.21},
    "Seoul":         {"lat": 37.57,  "lon": 126.98},
    "Singapore":     {"lat": 1.35,   "lon": 103.82},
    "Bangkok":       {"lat": 13.75,  "lon": 100.52},
    "Dubai":         {"lat": 25.20,  "lon": 55.27},
    # América Latina
    "Buenos Aires":  {"lat": -34.60, "lon": -58.38},
    "Sao Paulo":     {"lat": -23.55, "lon": -46.63},
    "Mexico City":   {"lat": 19.43,  "lon": -99.13},
    "Bogota":        {"lat": 4.71,   "lon": -74.07},
    "Lima":          {"lat": -12.05, "lon": -77.04},
    "Santiago":      {"lat": -33.46, "lon": -70.65},
    # Oceanía / África
    "Sydney":        {"lat": -33.87, "lon": 151.21},
    "Melbourne":     {"lat": -37.81, "lon": 144.96},
    "Lagos":         {"lat": 6.45,   "lon": 3.40},
    "Cairo":         {"lat": 30.06,  "lon": 31.25},
    "Nairobi":       {"lat": -1.29,  "lon": 36.82},
}

def get_precipitacion_prob(ciudad):
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
        return prob / 100.0
    except Exception as e:
        print(f"⚠️ [CLIMA] Error Open-Meteo para {ciudad}: {e}")
        return None

def get_mercados_polymarket(keywords):
    """Busca mercados filtrando por pregunta localmente — más confiable que el search de Polymarket"""
    mercados = []
    try:
        # Traer mercados activos ordenados por volumen
        url = "https://gamma-api.polymarket.com/markets?active=true&closed=false&limit=100&order=volume&ascending=false"
        r = requests.get(url, timeout=10)
        data = r.json()
        if isinstance(data, list):
            # Filtrar localmente por keywords en la pregunta
            for m in data:
                pregunta = m.get("question", "").lower()
                if any(kw.lower() in pregunta for kw in keywords):
                    mercados.append(m)
    except Exception as e:
        print(f"⚠️ Error obteniendo mercados: {e}")
    return mercados

def get_precio_yes(mercado):
    try:
        tokens = mercado.get("tokens", [])
        for token in tokens:
            if token.get("outcome", "").upper() == "YES":
                return float(token.get("price", 0))
        return float(mercado.get("bestAsk", 0) or mercado.get("lastTradePrice", 0))
    except:
        return None

def analizar_mercado_clima(mercado):
    pregunta = mercado.get("question", "").lower()
    precio_yes = get_precio_yes(mercado)
    if not precio_yes or precio_yes <= 0:
        return None

    ciudad_detectada = None
    for ciudad in CIUDADES.keys():
        if ciudad.lower() in pregunta:
            ciudad_detectada = ciudad
            break
    if not ciudad_detectada:
        return None

    # Verificar que la pregunta sea realmente sobre clima
    palabras_clima = ["rain", "rainfall", "precipitation", "storm", "snow", "flood", "temperature", "weather", "hurricane", "tornado", "degrees", "celsius", "fahrenheit", "humid", "wind"]
    if not any(w in pregunta for w in palabras_clima):
        return None

    prob_real = get_precipitacion_prob(ciudad_detectada)
    if prob_real is None:
        return None

    edge_yes = prob_real - precio_yes
    edge_no  = (1 - prob_real) - (1 - precio_yes)

    print(f"📊 [CLIMA] {ciudad_detectada} | Mercado: {precio_yes:.2f} | Open-Meteo: {prob_real:.2f} | Edge YES: {edge_yes:.2f}")

    if edge_yes >= EDGE_THRESHOLD:
        return ("yes", edge_yes)
    elif edge_no >= EDGE_THRESHOLD:
        return ("no", edge_no)
    return None

mercados_clima_apostados = set()

def motor_climatico():
    print("🌦️ [CLIMA] Motor climático iniciado")
    while True:
        try:
            mercados = get_mercados_polymarket(["weather", "temperature", "hurricane", "tornado", "flood", "snowfall"])
            print(f"🌦️ [CLIMA] {len(mercados)} mercados encontrados")
            for m in mercados[:5]:
                print(f"🌦️ [CLIMA] Mercado: {m.get('question', '')[:80]} | keys: {list(m.keys())[:8]}")
            for mercado in mercados:
                market_id = mercado.get("conditionId") or mercado.get("id")
                if not market_id or market_id in mercados_clima_apostados:
                    continue
                resultado = analizar_mercado_clima(mercado)
                if resultado:
                    side, edge = resultado
                    pregunta = mercado.get("question", "")[:60]
                    print(f"🎯 [CLIMA] Edge! {side.upper()} | Edge: {edge:.2f} | {pregunta}")
                    ok = ejecutar_trade(
                        market_id=market_id,
                        side=side,
                        razon=f"Bot climático | Edge: {edge:.2f}",
                        precio_ref=edge,
                        slug=mercado.get("slug")
                    )
                    if ok:
                        mercados_clima_apostados.add(market_id)
            time.sleep(600)
        except Exception as e:
            print(f"❌ [CLIMA] Error general: {e}")
            time.sleep(60)

# ============================================================
# MOTOR 3: BOT CRYPTO
# ============================================================

# Distribución normal acumulada (sin scipy)
def norm_cdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))

def get_precio_crypto(symbol):
    """Precio actual desde CoinGecko (gratuito, sin API key)"""
    ids = {
        "BTC": "bitcoin",
        "ETH": "ethereum",
        "SOL": "solana",
    }
    cg_id = ids.get(symbol)
    if not cg_id:
        return None
    try:
        url = f"https://api.coingecko.com/api/v3/simple/price?ids={cg_id}&vs_currencies=usd"
        r = requests.get(url, timeout=10)
        return float(r.json()[cg_id]["usd"])
    except Exception as e:
        print(f"⚠️ [CRYPTO] Error precio {symbol}: {e}")
        return None

def get_volatilidad_crypto(symbol):
    """Volatilidad diaria desde Binance (gratuito, sin API key)"""
    pairs = {"BTC": "BTCUSDT", "ETH": "ETHUSDT", "SOL": "SOLUSDT"}
    pair = pairs.get(symbol)
    if not pair:
        return 0.03  # fallback 3% diario
    try:
        url = f"https://api.binance.com/api/v3/klines?symbol={pair}&interval=1d&limit=14"
        r = requests.get(url, timeout=10)
        candles = r.json()
        retornos = []
        for i in range(1, len(candles)):
            c_prev = float(candles[i-1][4])
            c_curr = float(candles[i][4])
            if c_prev > 0:
                retornos.append(math.log(c_curr / c_prev))
        if not retornos:
            return 0.03
        media = sum(retornos) / len(retornos)
        varianza = sum((r - media) ** 2 for r in retornos) / len(retornos)
        return math.sqrt(varianza)
    except Exception as e:
        print(f"⚠️ [CRYPTO] Error volatilidad {symbol}: {e}")
        return 0.03

def prob_superar_precio(precio_actual, precio_objetivo, volatilidad_diaria, dias=1):
    """
    Probabilidad de que el precio supere precio_objetivo en `dias` días.
    Usa distribución log-normal.
    """
    if precio_actual <= 0 or precio_objetivo <= 0:
        return None
    sigma = volatilidad_diaria * math.sqrt(dias)
    mu = -0.5 * sigma ** 2  # drift neutro al riesgo
    d = (math.log(precio_objetivo / precio_actual) - mu) / sigma
    return 1 - norm_cdf(d)

def parsear_mercado_crypto(mercado):
    """
    Intenta extraer símbolo y precio objetivo de la pregunta del mercado.
    Ej: "Will BTC close above $85,000 this week?"
    """
    pregunta = mercado.get("question", "")
    pregunta_lower = pregunta.lower()

    symbol = None
    for s in ["BTC", "ETH", "SOL"]:
        if s.lower() in pregunta_lower or s in pregunta:
            symbol = s
            break
    if not symbol:
        return None, None

    # Extraer número del precio objetivo
    import re
    numeros = re.findall(r'[\$]?([\d,]+(?:\.\d+)?)[kK]?', pregunta)
    precio_objetivo = None
    for n in numeros:
        try:
            val = float(n.replace(",", ""))
            # Filtrar valores razonables según símbolo
            if symbol == "BTC" and 10000 < val < 500000:
                precio_objetivo = val
                break
            elif symbol == "ETH" and 500 < val < 50000:
                precio_objetivo = val
                break
            elif symbol == "SOL" and 10 < val < 5000:
                precio_objetivo = val
                break
        except:
            continue

    return symbol, precio_objetivo

mercados_crypto_apostados = set()

def motor_crypto():
    print("₿ [CRYPTO] Motor crypto iniciado")
    while True:
        try:
            mercados = get_mercados_polymarket(["bitcoin", "BTC", "ethereum", "ETH"])
            print(f"₿ [CRYPTO] {len(mercados)} mercados encontrados")

            for mercado in mercados:
                market_id = mercado.get("conditionId") or mercado.get("id")
                if not market_id or market_id in mercados_crypto_apostados:
                    continue

                pregunta = mercado.get("question", "")
                pregunta_lower = pregunta.lower()

                # Solo mercados tipo "above/below/close above"
                if not any(w in pregunta_lower for w in ["above", "below", "exceed", "surpass", "reach"]):
                    continue

                symbol, precio_objetivo = parsear_mercado_crypto(mercado)
                if not symbol or not precio_objetivo:
                    continue

                precio_actual = get_precio_crypto(symbol)
                if not precio_actual:
                    continue

                volatilidad = get_volatilidad_crypto(symbol)
                precio_yes = get_precio_yes(mercado)
                if not precio_yes or precio_yes <= 0:
                    continue

                # Calcular probabilidad real
                es_above = any(w in pregunta_lower for w in ["above", "exceed", "surpass", "reach"])
                prob_real = prob_superar_precio(precio_actual, precio_objetivo, volatilidad)
                if prob_real is None:
                    continue

                if not es_above:
                    prob_real = 1 - prob_real  # invertir para "below"

                edge = prob_real - precio_yes
                print(f"📊 [CRYPTO] {symbol} ${precio_actual:,.0f} → ${precio_objetivo:,.0f} | Mercado: {precio_yes:.2f} | Real: {prob_real:.2f} | Edge: {edge:.2f}")

                if abs(edge) >= EDGE_THRESHOLD:
                    side = "yes" if edge > 0 else "no"
                    print(f"🎯 [CRYPTO] Edge! {side.upper()} | {pregunta[:60]}")
                    ok = ejecutar_trade(
                        market_id=market_id,
                        side=side,
                        razon=f"Bot crypto {symbol} | Edge: {edge:.2f}",
                        precio_ref=f"{precio_actual} → {precio_objetivo}"
                    )
                    if ok:
                        mercados_crypto_apostados.add(market_id)

                time.sleep(2)

            # Revisar cada 15 minutos
            time.sleep(900)

        except Exception as e:
            print(f"❌ [CRYPTO] Error general: {e}")
            time.sleep(60)

# ============================================================
# MAIN
# ============================================================
print("🤖 Bot iniciado con 3 motores en paralelo")
print(f"👀 Copy trading: {len(TRADERS)} traders | Rango precio: {COPY_MIN_PRICE}-{COPY_MAX_PRICE}")
print(f"🌦️ Bot climático: Open-Meteo | Edge mínimo: {EDGE_THRESHOLD*100:.0f}%")
print(f"₿  Bot crypto: CoinGecko + Binance | Edge mínimo: {EDGE_THRESHOLD*100:.0f}%")
print(f"💰 Stake fijo: ${STAKE}")
notify(
    f"🤖 Bot iniciado con 3 motores!\n"
    f"💰 Stake: ${STAKE}\n"
    f"👀 Copiando {len(TRADERS)} traders\n"
    f"🌦️ Bot climático activo\n"
    f"₿ Bot crypto activo"
)

t1 = threading.Thread(target=motor_copy_trading, daemon=True)
t2 = threading.Thread(target=motor_climatico, daemon=True)
t3 = threading.Thread(target=motor_crypto, daemon=True)

t1.start()
t2.start()
t3.start()

while True:
    time.sleep(60)
