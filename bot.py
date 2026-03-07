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
raw_wallets = os.environ.get("SIMMER_COPYTRADING_WALLETS", "")
TRADERS = [w.strip() for w in raw_wallets.split(",") if w.strip()]

EDGE_THRESHOLD = float(os.environ.get("EDGE_THRESHOLD", "0.05"))
EDGE_THRESHOLD_CRYPTO = float(os.environ.get("EDGE_THRESHOLD_CRYPTO", "0.10"))  # más alto para crypto
EDGE_THRESHOLD_SPORTS = float(os.environ.get("EDGE_THRESHOLD_SPORTS", "0.07"))  # 7% para deportes
ODDS_API_KEY = os.environ.get("ODDS_API_KEY")
COPY_MIN_PRICE = 0.20
COPY_MAX_PRICE = 0.75

# === GESTIÓN DE RIESGO ===
STAKE = float(os.environ.get("MAX_USD", "1"))          # $1 por trade
MAX_TRADES_ABIERTOS = 3                                 # máximo 3 posiciones simultáneas
MAX_PORCENTAJE_SALDO = 0.30                             # no más del 30% del saldo total
SALDO_INICIAL = 7.82                                    # saldo actual en USDC.e

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

# === PROXY ===
PROXY_URL = os.environ.get("PROXY_URL")
if PROXY_URL:
    os.environ["HTTP_PROXY"] = PROXY_URL
    os.environ["HTTPS_PROXY"] = PROXY_URL
    print(f"🌐 Proxy configurado: {PROXY_URL[:30]}...")
else:
    print("⚠️ Sin proxy configurado")

# === SIMMER CLIENT ===
client = SimmerClient(api_key=SIMMER_API_KEY, venue="polymarket")

# === APPROVALS (una sola vez al iniciar) ===
try:
    print("🔓 Configurando approvals de Polymarket...")
    client.set_approvals()
    print("✅ Approvals configurados")
except Exception as e:
    print(f"⚠️ Error en approvals: {e}")

# === LOCK para evitar trades simultáneos ===
trade_lock = threading.Lock()
trades_copiados = set()

def importar_mercado(condition_id, slug=None):
    """Importa un mercado de Polymarket a Simmer"""
    urls_a_probar = []
    if slug:
        urls_a_probar.append(f"https://polymarket.com/event/{slug}")
        urls_a_probar.append(f"https://polymarket.com/market/{slug}")
    urls_a_probar.append(f"https://polymarket.com/event/{condition_id}")

    for url in urls_a_probar:
        try:
            print(f"🔄 Importando: {url}")
            result = client.import_market(url)
            print(f"📥 Respuesta import: {result}")
            if isinstance(result, dict):
                simmer_id = result.get("id") or result.get("market_id") or result.get("conditionId")
                if simmer_id:
                    print(f"✅ Mercado importado: {simmer_id}")
                    return simmer_id
        except Exception as e:
            print(f"⚠️ Error importando {url}: {e}")
    return None

trades_abiertos = 0

def ejecutar_trade(market_id, side, razon, precio_ref=None, slug=None):
    global trades_abiertos
    with trade_lock:
        # === CHEQUEO DE RIESGO ===
        if trades_abiertos >= MAX_TRADES_ABIERTOS:
            print(f"⛔ Riesgo: máximo {MAX_TRADES_ABIERTOS} trades abiertos alcanzado, skip")
            return False
        gasto_actual = trades_abiertos * STAKE
        if (gasto_actual + STAKE) > (SALDO_INICIAL * MAX_PORCENTAJE_SALDO):
            print(f"⛔ Riesgo: límite del {MAX_PORCENTAJE_SALDO*100:.0f}% del saldo alcanzado, skip")
            return False
        try:
            # Importar mercado a Simmer y obtener su ID
            simmer_id = importar_mercado(market_id, slug=slug)
            trade_id = simmer_id or market_id

            # Garantizar mínimo 5 shares para Polymarket
            precio_impl = precio_ref if isinstance(precio_ref, float) and 0 < precio_ref < 1 else 0.5
            monto_minimo_shares = round(5 * precio_impl + 0.10, 2)  # 5 shares + margen chico
            monto_final = max(STAKE, monto_minimo_shares)

            # Verificar que el monto final no exceda el presupuesto disponible
            presupuesto_disponible = round(SALDO_INICIAL * MAX_PORCENTAJE_SALDO - gasto_actual, 2)
            if monto_final > presupuesto_disponible:
                print(f"⏭️ Skip: necesita ${monto_final} pero presupuesto disponible es ${presupuesto_disponible}")
                return False

            trades_abiertos += 1
            # Slippage: subir 2% el precio para cruzar el spread y asegurar fill
            precio_con_slippage = None
            if precio_impl and 0 < precio_impl < 1:
                precio_con_slippage = round(min(precio_impl * 1.02, 0.95), 4)

            print(f"💵 Monto trade: ${monto_final} (precio ref: {precio_impl:.2f} → con slippage: {precio_con_slippage})")
            trade_kwargs = dict(market_id=trade_id, side=side, amount=monto_final, order_type="GTC")
            if precio_con_slippage:
                trade_kwargs["price"] = precio_con_slippage
            result = client.trade(**trade_kwargs)
            if hasattr(result, 'success') and result.success:
                msg = (
                    f"✅ Trade ejecutado!\n"
                    f"Razón: {razon}\n"
                    f"Side: {side.upper()}\n"
                    f"Precio ref: {precio_ref}\n"
                    f"Stake: ${STAKE}\n"
                    f"Trades abiertos: {trades_abiertos}/{MAX_TRADES_ABIERTOS}\n"
                    f"Resultado: {result}"
                )
                print(msg)
                notify(msg)
                return True
            else:
                trades_abiertos -= 1  # revertir si falló
                err = f"❌ Trade fallido: {getattr(result, 'error', result)}"
                print(err)
                notify(err)
                return False
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

    # Filtro: mercados extremos (< 0.10 o > 0.90) suelen tener razón, Open-Meteo no es suficiente
    if precio_yes < 0.10 or precio_yes > 0.90:
        print(f"⏭️ [CLIMA] Skip: precio extremo {precio_yes:.2f}, mercado probablemente correcto")
        return None

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
                print(f"🌦️ [CLIMA] Mercado: {m.get('question', '')[:80]}")
            for mercado in mercados:
                market_id = mercado.get("conditionId") or mercado.get("id")
                if not market_id or market_id in mercados_clima_apostados:
                    continue
                resultado = analizar_mercado_clima(mercado)
                if resultado:
                    side, edge = resultado
                    pregunta = mercado.get("question", "")[:60]
                    print(f"🎯 [CLIMA] Edge! {side.upper()} | Edge: {edge:.2f} | {pregunta}")
                    precio_yes = get_precio_yes(mercado)
                    precio_side = precio_yes if side == "yes" else (1 - precio_yes)
                    ok = ejecutar_trade(
                        market_id=market_id,
                        side=side,
                        razon=f"Bot climático | Edge: {edge:.2f}",
                        precio_ref=precio_side,
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

                if abs(edge) >= EDGE_THRESHOLD_CRYPTO:
                    side = "yes" if edge > 0 else "no"
                    print(f"🎯 [CRYPTO] Edge! {side.upper()} | {pregunta[:60]}")
                    precio_side = precio_yes if side == "yes" else (1 - precio_yes)
                    ok = ejecutar_trade(
                        market_id=market_id,
                        side=side,
                        razon=f"Bot crypto {symbol} | Edge: {edge:.2f}",
                        precio_ref=precio_side
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
# MOTOR 4: BOT DEPORTES (The Odds API + Polymarket)
# ============================================================

# Deportes a monitorear
DEPORTES = [
    "soccer_epl",           # Premier League
    "soccer_uefa_champs_league",  # Champions League
    "soccer_argentina_primera_division",  # Liga Argentina
    "basketball_nba",       # NBA
    "americanfootball_nfl", # NFL
    "baseball_mlb",         # MLB
]

def get_odds_deportes():
    """Obtiene probabilidades de casas de apuestas desde The Odds API"""
    if not ODDS_API_KEY:
        return []
    partidos = []
    deportes_validos = set(DEPORTES)  # solo ligas configuradas
    for deporte in DEPORTES:
        try:
            url = (
                f"https://api.the-odds-api.com/v4/sports/{deporte}/odds"
                f"?apiKey={ODDS_API_KEY}&regions=eu&markets=h2h&oddsFormat=decimal&dateFormat=iso"
            )
            r = requests.get(url, timeout=10)
            if r.status_code == 200:
                data = r.json()
                for partido in data:
                    partido["_sport"] = deporte
                    partidos.append(partido)
            elif r.status_code == 401:
                print(f"⚠️ [DEPORTES] API key inválida")
                break
            time.sleep(0.5)
        except Exception as e:
            print(f"⚠️ [DEPORTES] Error obteniendo odds de {deporte}: {e}")
    return partidos

def decimal_a_prob(odd):
    """Convierte odds decimales a probabilidad implícita"""
    if odd <= 0:
        return 0
    return 1 / odd

def get_prob_casa_apuestas(partido):
    """
    Calcula probabilidad promedio entre todas las casas para home/away/draw.
    Retorna {home_prob, away_prob, draw_prob} sin vig.
    """
    bookmakers = partido.get("bookmakers", [])
    if not bookmakers:
        return None

    home_probs, away_probs, draw_probs = [], [], []

    for bm in bookmakers:
        for market in bm.get("markets", []):
            if market.get("key") != "h2h":
                continue
            outcomes = market.get("outcomes", [])
            home_name = partido.get("home_team", "")
            away_name = partido.get("away_team", "")
            for outcome in outcomes:
                p = decimal_a_prob(outcome.get("price", 0))
                if outcome.get("name") == home_name:
                    home_probs.append(p)
                elif outcome.get("name") == away_name:
                    away_probs.append(p)
                else:
                    draw_probs.append(p)

    if not home_probs or not away_probs:
        return None

    # Promedio y normalización (remover vig)
    home_raw = sum(home_probs) / len(home_probs)
    away_raw = sum(away_probs) / len(away_probs)
    draw_raw = sum(draw_probs) / len(draw_probs) if draw_probs else 0
    total = home_raw + away_raw + draw_raw or 1

    return {
        "home": home_raw / total,
        "away": away_raw / total,
        "draw": draw_raw / total if draw_probs else 0,
    }

def buscar_mercado_partido_polymarket(home, away):
    """Busca en Polymarket el mercado ganador del partido usando nombres de equipos"""
    try:
        url = "https://gamma-api.polymarket.com/markets?active=true&closed=false&limit=100&order=volume&ascending=false"
        r = requests.get(url, timeout=10)
        mercados = r.json() if isinstance(r.json(), list) else []
        home_words = home.lower().split()[:2]
        away_words = away.lower().split()[:2]
        # Palabras que indican mercado de apuestas de sets/games (excluir)
        excluir_keywords = ["o/u", "over", "under", "set ", "game", "handicap", "spread", "total", "quarter", "half"]
        candidatos = []
        for m in mercados:
            pregunta = m.get("question", "").lower()
            if any(w in pregunta for w in home_words) and any(w in pregunta for w in away_words):
                # Preferir mercados de ganador directo (sin sets/totales)
                tiene_excluido = any(ex in pregunta for ex in excluir_keywords)
                candidatos.append((m, tiene_excluido))
        # Primero los que NO tienen keywords de excluir
        candidatos.sort(key=lambda x: x[1])
        if candidatos:
            return candidatos[0][0]
    except Exception as e:
        print(f"⚠️ [DEPORTES] Error buscando mercado: {e}")
    return None

mercados_deportes_apostados = set()

def motor_deportes():
    print("⚽ [DEPORTES] Motor de deportes iniciado")
    while True:
        try:
            if not ODDS_API_KEY:
                print("⚠️ [DEPORTES] Sin ODDS_API_KEY, motor pausado")
                time.sleep(3600)
                continue

            partidos = get_odds_deportes()
            print(f"⚽ [DEPORTES] {len(partidos)} partidos encontrados")

            for partido in partidos:
                home = partido.get("home_team", "")
                away = partido.get("away_team", "")
                partido_id = partido.get("id", "")

                if partido_id in mercados_deportes_apostados:
                    continue

                probs = get_prob_casa_apuestas(partido)
                if not probs:
                    continue

                # Buscar mercado en Polymarket
                mercado = buscar_mercado_partido_polymarket(home, away)
                if not mercado:
                    continue

                precio_yes = get_precio_yes(mercado)
                if not precio_yes or precio_yes <= 0:
                    continue

                market_id = mercado.get("conditionId") or mercado.get("id")
                pregunta = mercado.get("question", "")

                # Comparar probabilidad de casa (home win = YES en la mayoría de mercados)
                prob_real = probs["home"]
                edge = prob_real - precio_yes

                print(f"⚽ [DEPORTES] {home} vs {away} | Casas: {prob_real:.2f} | Poly: {precio_yes:.2f} | Edge: {edge:.2f}")

                if abs(edge) >= EDGE_THRESHOLD_SPORTS:
                    side = "yes" if edge > 0 else "no"
                    print(f"🎯 [DEPORTES] Edge! {side.upper()} | {pregunta[:60]}")
                    ok = ejecutar_trade(
                        market_id=market_id,
                        side=side,
                        razon=f"Bot deportes {home} vs {away} | Edge: {edge:.2f}",
                        precio_ref=precio_yes if side == "yes" else (1 - precio_yes),
                        slug=mercado.get("slug")
                    )
                    if ok:
                        mercados_deportes_apostados.add(partido_id)

                time.sleep(1)

            # Revisar cada 20 minutos
            time.sleep(1200)

        except Exception as e:
            print(f"❌ [DEPORTES] Error general: {e}")
            time.sleep(60)

# ============================================================
# MAIN
# ============================================================
print("🤖 Bot iniciado con 4 motores en paralelo")
print(f"👀 Copy trading: {len(TRADERS)} traders | Rango precio: {COPY_MIN_PRICE}-{COPY_MAX_PRICE}")
print(f"🌦️ Bot climático: Open-Meteo | Edge mínimo: {EDGE_THRESHOLD*100:.0f}%")
print(f"₿  Bot crypto: CoinGecko + Binance | Edge mínimo: {EDGE_THRESHOLD_CRYPTO*100:.0f}%")
print(f"⚽ Bot deportes: The Odds API | Edge mínimo: {EDGE_THRESHOLD_SPORTS*100:.0f}%")
print(f"💰 Stake fijo: ${STAKE}")
notify(
    f"🤖 Bot iniciado con 4 motores!\n"
    f"💰 Stake: ${STAKE}\n"
    f"👀 Copiando {len(TRADERS)} traders\n"
    f"🌦️ Bot climático activo\n"
    f"₿ Bot crypto activo\n"
    f"⚽ Bot deportes activo"
)

t1 = threading.Thread(target=motor_copy_trading, daemon=True)
t2 = threading.Thread(target=motor_climatico, daemon=True)
t3 = threading.Thread(target=motor_crypto, daemon=True)
t4 = threading.Thread(target=motor_deportes, daemon=True)

t1.start()
t2.start()
t3.start()
t4.start()

while True:
    time.sleep(60)
