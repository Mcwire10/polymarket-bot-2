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

EDGE_THRESHOLD = float(os.environ.get("EDGE_THRESHOLD", "0.12"))  # 12% mínimo para clima — Open-Meteo no es oráculo
EDGE_THRESHOLD_CRYPTO = float(os.environ.get("EDGE_THRESHOLD_CRYPTO", "0.10"))  # más alto para crypto
EDGE_THRESHOLD_SPORTS = float(os.environ.get("EDGE_THRESHOLD_SPORTS", "0.07"))  # 7% para deportes
ODDS_API_KEY = os.environ.get("ODDS_API_KEY")
COPY_MIN_PRICE = 0.10  # ampliado para no perderse trades buenos
COPY_MAX_PRICE = 0.85

# === WALLET ===
WALLET_ADDRESS = os.environ.get("POLY_WALLET_ADDR", "")  # dirección pública de la wallet

# === TRACKING DIARIO ===
from datetime import datetime, timezone
reporte_lock = threading.Lock()
senales_del_dia = []   # {"motor", "mercado", "side", "edge", "hora"}
trades_del_dia = []    # {"motor", "mercado", "side", "monto", "hora", "ok"}

def registrar_senal(motor, mercado, side, edge):
    with reporte_lock:
        senales_del_dia.append({
            "motor": motor, "mercado": mercado[:60],
            "side": side, "edge": round(edge, 2),
            "hora": datetime.now(timezone.utc).strftime("%H:%M")
        })

def registrar_trade(motor, mercado, side, monto, ok):
    with reporte_lock:
        trades_del_dia.append({
            "motor": motor, "mercado": mercado[:60],
            "side": side, "monto": monto, "ok": ok,
            "hora": datetime.now(timezone.utc).strftime("%H:%M")
        })

# === GESTIÓN DE RIESGO ===
STAKE = float(os.environ.get("MAX_USD", "1"))          # $1 por trade
MAX_TRADES_ABIERTOS = 2                                 # máximo 2 posiciones — conservador mientras saldo < $5
MAX_PORCENTAJE_SALDO = 0.50                             # no más del 50% del saldo total
SALDO_INICIAL = 5.56                                    # saldo actual en USDC.e (se actualiza al arrancar)

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
WALLET_PRIVATE_KEY = os.environ.get("WALLET_PRIVATE_KEY", "").strip()
# Simmer requiere prefijo 0x
if WALLET_PRIVATE_KEY and not WALLET_PRIVATE_KEY.startswith("0x"):
    WALLET_PRIVATE_KEY = "0x" + WALLET_PRIVATE_KEY
print(f"🔑 Private key: {'OK (' + str(len(WALLET_PRIVATE_KEY)) + ' chars, con 0x)' if WALLET_PRIVATE_KEY else 'NO ENCONTRADA'}")
client = SimmerClient(
    api_key=SIMMER_API_KEY,
    venue="polymarket",
    private_key=WALLET_PRIVATE_KEY if WALLET_PRIVATE_KEY else None
)

def get_saldo_wallet():
    """Consulta el saldo USDC.e de la wallet en Polygon via RPC público"""
    if not WALLET_ADDRESS:
        print(f"⚠️ [SALDO] POLY_WALLET_ADDR no configurada")
        return None
    try:
        USDC_E_CONTRACT = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
        addr_clean = WALLET_ADDRESS.lower().replace("0x", "").zfill(64)
        data_call = "0x70a08231" + addr_clean
        payload = {
            "jsonrpc": "2.0", "method": "eth_call",
            "params": [{"to": USDC_E_CONTRACT, "data": data_call}, "latest"],
            "id": 1
        }
        # RPC sin proxy — los RPCs públicos no necesitan proxy, es tráfico de lectura
        RPC_ENDPOINTS = [
            "https://rpc-mainnet.matic.quiknode.pro",
            "https://matic-mainnet.chainstacklabs.com",
            "https://rpc.ankr.com/polygon",
        ]
        r = None
        for rpc in RPC_ENDPOINTS:
            try:
                print(f"💰 [SALDO] Consultando {WALLET_ADDRESS[:10]}... via {rpc.split('/')[2]}")
                r = requests.post(rpc, json=payload, timeout=8)
                if r.status_code == 200 and "result" in r.json():
                    break
                print(f"💰 [SALDO] {rpc.split('/')[2]} falló: {r.text[:80]}")
            except Exception as e:
                print(f"💰 [SALDO] {rpc} error: {e}")
                r = None
        if not r:
            return None
        print(f"💰 [SALDO] Respuesta: {r.text[:80]}")
        result = r.json().get("result", "0x0")
        raw = int(result, 16)
        saldo = round(raw / 1_000_000, 2)
        print(f"💰 [SALDO] Saldo calculado: ${saldo}")
        return saldo
    except Exception as e:
        print(f"⚠️ [SALDO] Error: {e}")
    return None

def actualizar_saldo_inicial():
    """Actualiza SALDO_INICIAL con el saldo real de la wallet"""
    global SALDO_INICIAL
    saldo = get_saldo_wallet()
    if saldo and saldo > 0:
        SALDO_INICIAL = saldo
        print(f"💰 Saldo actualizado: ${SALDO_INICIAL} USDC.e")
    else:
        print(f"⚠️ No se pudo leer saldo, usando ${SALDO_INICIAL} (hardcoded)")

# === APPROVALS (una sola vez al iniciar) ===
try:
    print("🔓 Configurando approvals de Polymarket...")
    client.set_approvals()
    print("✅ Approvals configurados")
except Exception as e:
    print(f"⚠️ Error en approvals: {e}")

# Leer saldo real de la wallet al iniciar
actualizar_saldo_inicial()

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

            # Monto fijo = STAKE. Verificar mínimo 5 shares de Polymarket.
            precio_impl = precio_ref if isinstance(precio_ref, float) and 0 < precio_ref < 1 else 0.5
            monto_minimo = round(5 * precio_impl + 0.05, 2)  # 5 shares mínimo
            if monto_minimo > STAKE:
                # El mercado es muy caro para nuestro stake — skip
                print(f"⏭️ Skip: precio muy alto ({precio_impl:.2f}), necesita ${monto_minimo} para 5 shares mínimas")
                return False
            monto_final = max(STAKE, 1.10)  # mínimo $1.10 para cubrir fees y redondeos

            # Verificar que el monto final no exceda el presupuesto disponible
            presupuesto_disponible = round(SALDO_INICIAL * MAX_PORCENTAJE_SALDO - gasto_actual, 2)
            if monto_final > presupuesto_disponible:
                print(f"⏭️ Skip: necesita ${monto_final} pero presupuesto disponible es ${presupuesto_disponible}")
                return False

            # Slippage: subir 2% el precio para cruzar el spread y asegurar fill
            precio_con_slippage = None
            if precio_impl and 0 < precio_impl < 1:
                precio_con_slippage = round(min(precio_impl * 1.02, 0.95), 4)

            print(f"💵 Monto trade: ${monto_final} (precio ref: {precio_impl:.2f} → con slippage: {precio_con_slippage})")
            trades_abiertos += 1
            trade_kwargs = dict(market_id=trade_id, side=side, amount=monto_final, order_type="GTC", allow_rebuy=True)
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
                registrar_trade(razon.split()[0], str(market_id)[:60], side, monto_final, ok=True)
                return True
            else:
                trades_abiertos -= 1  # revertir si falló
                err = f"❌ Trade fallido: {getattr(result, 'error', result)}"
                print(err)
                notify(err)
                registrar_trade(razon.split()[0], str(market_id)[:60], side, monto_final, ok=False)
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

                    # Debug: loguear todos los trades que llegan
                    print(f"🔍 [COPY] Trade raw: id={trade_id[:8]} side={side} price={price} asset={str(asset)[:20]}")
                    if side not in ("BUY", "YES", "BUY_YES", "LONG"):
                        print(f"⏭️ [COPY] Skip side={side}")
                        continue
                    if not (COPY_MIN_PRICE <= price <= COPY_MAX_PRICE):
                        print(f"⏭️ [COPY] Precio {price} fuera de rango, skip")
                        continue

                    # Filtro de frescura: solo copiar trades de las últimas 2 horas
                    from datetime import datetime, timezone, timedelta
                    timestamp = trade.get("timestamp") or trade.get("createdAt") or trade.get("time")
                    if timestamp:
                        try:
                            if isinstance(timestamp, (int, float)):
                                trade_dt = datetime.fromtimestamp(timestamp / 1000 if timestamp > 1e10 else timestamp, tz=timezone.utc)
                            else:
                                trade_dt = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
                            edad = datetime.now(timezone.utc) - trade_dt
                            if edad > timedelta(minutes=15):
                                print(f"⏭️ [COPY] Trade de {wallet[:8]} tiene {int(edad.total_seconds()//60)}min, muy viejo (máx 15min), skip")
                                continue
                        except Exception as e:
                            print(f"⚠️ [COPY] No se pudo parsear timestamp: {e}")

                    # Filtro volumen: solo mercados con > $50k para no comprar en el techo
                    volumen = float(trade.get("volume") or trade.get("volume24hr") or trade.get("liquidity") or 0)
                    if volumen > 0 and volumen < 50000:
                        print(f"⏭️ [COPY] Volumen ${volumen:.0f} < $50k, skip")
                        continue

                    # Filtro slippage: precio actual no debe ser > 3% más caro que el del trader
                    precio_actual = price  # se refinará en ejecutar_trade
                    slippage_max = price * 1.03
                    print(f"🔔 [COPY] Trade detectado de {wallet[:8]}... | Price: {price} | Slippage máx: {slippage_max:.3f}")
                    ejecutar_trade(
                        market_id=asset,
                        side="yes",
                        razon=f"Copy de {wallet[:8]} @ {price}",
                        precio_ref=price,
                        precio_max=slippage_max
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

def get_temperatura_max(ciudad):
    """Obtiene temperatura máxima pronosticada en °C y °F"""
    coords = CIUDADES.get(ciudad)
    if not coords:
        return None, None
    url = (
        f"https://api.open-meteo.com/v1/forecast"
        f"?latitude={coords['lat']}&longitude={coords['lon']}"
        f"&daily=temperature_2m_max"
        f"&forecast_days=1&timezone=auto"
        f"&temperature_unit=celsius"
    )
    try:
        r = requests.get(url, timeout=10)
        data = r.json()
        temp_c = data["daily"]["temperature_2m_max"][0]
        temp_f = round(temp_c * 9/5 + 32, 1)
        return round(temp_c, 1), temp_f
    except Exception as e:
        print(f"⚠️ [CLIMA] Error temperatura para {ciudad}: {e}")
        return None, None

def prob_temperatura_bucket(temp_real, temp_objetivo, margen=0.7, sigma_override=None):
    """
    Probabilidad real usando distribución normal.
    sigma varía según horizonte: D+0=0.6°C, D+1=1.5°C, D+2=2.0°C (recomendación Gemini)
    """
    if temp_real is None:
        return None
    if sigma_override is not None:
        sigma = sigma_override
    else:
        # Fallback: detectar por escala de temperatura
        sigma = 2.7 if abs(temp_objetivo) > 40 else 1.5
    limite_inf = temp_objetivo - margen
    limite_sup = temp_objetivo + margen
    # P(limite_inf <= X <= limite_sup) donde X ~ N(temp_real, sigma)
    z_inf = (limite_inf - temp_real) / sigma
    z_sup = (limite_sup - temp_real) / sigma
    prob = norm_cdf(z_sup) - norm_cdf(z_inf)
    return round(prob, 3)

def get_mercados_polymarket(keywords):
    """Busca mercados filtrando por pregunta localmente — más confiable que el search de Polymarket"""
    mercados = []
    try:
        # Traer mercados activos ordenados por volumen
        url = "https://gamma-api.polymarket.com/markets?active=true&closed=false&limit=100&order=volume&ascending=false"
        r = requests.get(url, timeout=10)
        data = r.json()
        if isinstance(data, list):
            from datetime import datetime, timezone, timedelta
            limite_fecha = datetime.now(timezone.utc) + timedelta(days=7)
            for m in data:
                pregunta = m.get("question", "").lower()
                if not any(kw.lower() in pregunta for kw in keywords):
                    continue
                # Solo mercados que vencen en los próximos 7 días
                end_date_str = m.get("endDate") or m.get("end_date_iso") or m.get("endDateIso")
                if end_date_str:
                    try:
                        end_date = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
                        if end_date > limite_fecha:
                            continue
                    except:
                        pass
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
    from datetime import datetime, timezone
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

    # Detectar horizonte temporal para sigma variable (recomendación Gemini)
    horizonte_dias = 1  # default D+1
    try:
        end_date_str = mercado.get("endDate") or mercado.get("end_date") or mercado.get("closeTime") or ""
        if end_date_str:
            end_dt = datetime.fromisoformat(str(end_date_str).replace("Z", "+00:00"))
            horizonte_dias = max(0, (end_dt.date() - datetime.now(timezone.utc).date()).days)
    except:
        pass
    # Sigma según Gemini: D+0=0.6, D+1=1.5, D+2=2.0
    sigma_c = {0: 0.6, 1: 1.5, 2: 2.0}.get(horizonte_dias, 2.0)
    sigma_f = sigma_c * 1.8
    print(f"🌦️ [CLIMA] Horizonte D+{horizonte_dias} → sigma {sigma_c}°C/{sigma_f:.1f}°F")

    # Verificar que la pregunta sea realmente sobre clima
    palabras_clima = ["rain", "rainfall", "precipitation", "storm", "snow", "flood", "temperature", "weather", "hurricane", "tornado", "degrees", "celsius", "fahrenheit", "humid", "wind"]
    if not any(w in pregunta for w in palabras_clima):
        return None

    # Detectar tipo de mercado: temperatura o precipitación
    es_temperatura = any(w in pregunta for w in ["°c", "°f", "celsius", "fahrenheit", "degrees", "temperature", "highest temp", "high temp"])
    es_precipitacion = any(w in pregunta for w in ["rain", "rainfall", "precipitation", "storm", "snow", "flood"])

    if es_temperatura:
        # Extraer temperatura objetivo de la pregunta
        import re
        nums = re.findall(r'\d+(?:\.\d+)?', pregunta)
        if not nums:
            return None
        # Para "between 58-59", tomar el promedio
        if "between" in pregunta and len(nums) >= 2:
            temp_objetivo = (float(nums[0]) + float(nums[1])) / 2
        else:
            temp_objetivo = float(nums[0])
        # Determinar si es Celsius o Fahrenheit
        es_fahrenheit = "°f" in pregunta or "fahrenheit" in pregunta or "f on" in pregunta or "-" in pregunta and "f" in pregunta
        temp_c, temp_f = get_temperatura_max(ciudad_detectada)
        if temp_c is None:
            return None
        temp_comparar = temp_f if es_fahrenheit else temp_c
        sigma_usar = sigma_f if es_fahrenheit else sigma_c
        prob_real = prob_temperatura_bucket(temp_comparar, temp_objetivo, sigma_override=sigma_usar)
        print(f"📊 [CLIMA] {ciudad_detectada} | Temp real: {temp_comparar}{'°F' if es_fahrenheit else '°C'} | Objetivo: {temp_objetivo} | Prob bucket: {prob_real:.2f}")
    elif es_precipitacion:
        prob_real = get_precipitacion_prob(ciudad_detectada)
        print(f"📊 [CLIMA] {ciudad_detectada} | Mercado: {precio_yes:.2f} | Open-Meteo lluvia: {prob_real:.2f}")
    else:
        return None

    if prob_real is None:
        return None

    edge_yes = prob_real - precio_yes
    edge_no  = (1 - prob_real) - (1 - precio_yes)

    print(f"📊 [CLIMA] {ciudad_detectada} | Mercado: {precio_yes:.2f} | Open-Meteo: {prob_real:.2f} | Edge YES: {edge_yes:.2f}")

    # Filtro: mercados extremos (< 0.10 o > 0.90) suelen tener razón, Open-Meteo no es suficiente
    if precio_yes < 0.15 or precio_yes > 0.75:
        print(f"⏭️ [CLIMA] Skip: precio extremo {precio_yes:.2f}, mercado demasiado sesgado")
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
                    registrar_senal("clima", mercado.get("question",""), side, edge)
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
            # Esperar hasta el próximo ciclo GFS: 00:15, 06:15, 12:15, 18:15 UTC
            from datetime import datetime, timezone
            now = datetime.now(timezone.utc)
            hora = now.hour
            minuto = now.minute
            ciclos_gfs = [0, 6, 12, 18]
            proximos = []
            for c in ciclos_gfs:
                mins_desde_medianoche = c * 60 + 15
                mins_ahora = hora * 60 + minuto
                diff = mins_desde_medianoche - mins_ahora
                if diff <= 0:
                    diff += 24 * 60
                proximos.append(diff)
            espera_min = min(proximos)
            espera_seg = espera_min * 60
            print(f"🌦️ [CLIMA] Próximo ciclo GFS en {espera_min:.0f} min")
            time.sleep(min(espera_seg, 3600))  # máximo 1h de espera
        except Exception as e:
            print(f"❌ [CLIMA] Error general: {e}")
            time.sleep(300)

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
                    registrar_senal("crypto", pregunta, side, abs(edge))
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

        # Usar palabras de al menos 4 letras para evitar falsos matches
        home_words = [w for w in home.lower().split() if len(w) >= 4][:2]
        away_words = [w for w in away.lower().split() if len(w) >= 4][:2]

        if not home_words or not away_words:
            return None

        excluir_keywords = ["o/u", "over", "under", "set ", "handicap", "spread", "total", "quarter", "half", "prime minister", "president", "election", "political"]
        from datetime import datetime, timezone, timedelta
        limite_fecha = datetime.now(timezone.utc) + timedelta(days=7)  # solo mercados que vencen en 7 días
        candidatos = []
        for m in mercados:
            pregunta = m.get("question", "").lower()
            # Filtro de fecha de vencimiento
            end_date_str = m.get("endDate") or m.get("end_date_iso") or m.get("endDateIso")
            if end_date_str:
                try:
                    end_date = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
                    if end_date > limite_fecha:
                        continue  # vence en más de 7 días, skip
                except:
                    pass
            # Requiere match de AMBAS palabras de AMBOS equipos (más estricto)
            home_match = sum(1 for w in home_words if w in pregunta)
            away_match = sum(1 for w in away_words if w in pregunta)
            if home_match >= 1 and away_match >= 1:
                tiene_excluido = any(ex in pregunta for ex in excluir_keywords)
                es_partido = " vs" in pregunta or " versus" in pregunta
                if not tiene_excluido and es_partido:
                    candidatos.append(m)
        if candidatos:
            return candidatos[0]
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
                if not precio_yes or precio_yes <= 0.05 or precio_yes >= 0.95:
                    continue  # evita mercados extremos o campeonatos long-shot

                market_id = mercado.get("conditionId") or mercado.get("id")
                pregunta = mercado.get("question", "")

                # Comparar probabilidad de casa (home win = YES en la mayoría de mercados)
                prob_real = probs["home"]
                edge = prob_real - precio_yes

                print(f"⚽ [DEPORTES] {home} vs {away} | Casas: {prob_real:.2f} | Poly: {precio_yes:.2f} | Edge: {edge:.2f}")

                if abs(edge) >= EDGE_THRESHOLD_SPORTS:
                    side = "yes" if edge > 0 else "no"
                    registrar_senal("deportes", pregunta, side, abs(edge))
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
# MOTOR 5: SINCRONIZACIÓN DE POSICIONES
# ============================================================

# ============================================================
# MOTOR 5: SINCRONIZACIÓN DE POSICIONES
# ============================================================
def motor_sincronizacion():
    """Cada 30 min consulta las posiciones reales en Simmer y corrige trades_abiertos"""
    global trades_abiertos
    print("🔄 [SYNC] Motor de sincronización iniciado")
    time.sleep(60)  # esperar 1 min antes del primer check
    while True:
        try:
            # === AUTO-REDEEM: cobrar posiciones ganadoras ===
            try:
                redimidos = client.auto_redeem()
                if redimidos:
                    for r in redimidos:
                        if r.get("success"):
                            msg = f"💸 Auto-redeem! Mercado cobrado: {r.get('market_id','?')} | tx: {r.get('tx_hash','?')[:12]}"
                            print(msg)
                            notify(msg)
            except Exception as e:
                print(f"⚠️ [SYNC] Auto-redeem error: {e}")
            posiciones = client.get_positions()
            if posiciones is not None:
                if isinstance(posiciones, list):
                    print(f"🔄 [SYNC] {len(posiciones)} posiciones encontradas")
                    activas = []
                    for p in posiciones:
                        try:
                            # Debug: ver qué campos tiene el objeto
                            if hasattr(p, '__dict__'):
                                campos = {k: v for k, v in vars(p).items() if not k.startswith('_')}
                            elif isinstance(p, dict):
                                campos = p
                            else:
                                campos = {}
                            print(f"🔄 [SYNC] Posición: {str(campos)[:150]}")

                            # Intentar múltiples nombres de campo
                            resolved = (getattr(p, "resolved", None) or getattr(p, "is_resolved", None) or
                                       (p.get("resolved") or p.get("is_resolved") if isinstance(p, dict) else False))
                            value = (getattr(p, "currentValue", None) or getattr(p, "current_value", None) or
                                    getattr(p, "value", None) or getattr(p, "size", None) or
                                    (p.get("currentValue") or p.get("current_value") or p.get("value") if isinstance(p, dict) else 0))
                            if not resolved and float(value or 0) > 0:
                                activas.append(p)
                        except Exception as e:
                            print(f"⚠️ [SYNC] Error parseando posición: {e}")
                    nuevo_valor = min(len(activas), MAX_TRADES_ABIERTOS)
                else:
                    print(f"🔄 [SYNC] Posiciones no es lista: {type(posiciones)}")
                    nuevo_valor = 0

                # Si SDK devuelve 0 activas, verificar con REST API directamente
                if nuevo_valor == 0:
                    try:
                        headers = {"Authorization": f"Bearer {SIMMER_API_KEY}"}
                        r = requests.get(
                            f"https://api.simmer.market/api/sdk/positions?wallet={POLY_WALLET_ADDR}",
                            headers=headers, timeout=10
                        )
                        if r.status_code == 200:
                            data = r.json()
                            pos_list = data if isinstance(data, list) else data.get("positions", data.get("data", []))
                            activas_rest = []
                            for p in pos_list:
                                if isinstance(p, dict):
                                    resolved = p.get("resolved") or p.get("is_resolved") or p.get("outcome") is not None
                                    value = float(p.get("currentValue") or p.get("current_value") or p.get("value") or p.get("size") or 0)
                                    if not resolved and value > 0:
                                        activas_rest.append(p)
                                        print(f"🔄 [SYNC REST] Posición activa: {str(p)[:120]}")
                            if activas_rest:
                                nuevo_valor = min(len(activas_rest), MAX_TRADES_ABIERTOS)
                                print(f"🔄 [SYNC REST] Encontradas {len(activas_rest)} posiciones via REST")
                        else:
                            print(f"⚠️ [SYNC REST] Status {r.status_code}: {r.text[:100]}")
                    except Exception as e:
                        print(f"⚠️ [SYNC REST] Error: {e}")

                if nuevo_valor != trades_abiertos:
                    print(f"🔄 [SYNC] Corrigiendo trades_abiertos: {trades_abiertos} → {nuevo_valor} (posiciones activas reales)")
                    with trade_lock:
                        trades_abiertos = nuevo_valor
                else:
                    print(f"🔄 [SYNC] Posiciones OK: {trades_abiertos} trades abiertos")
            else:
                if trades_abiertos >= MAX_TRADES_ABIERTOS:
                    print(f"⚠️ [SYNC] No se pudo verificar posiciones, reseteando contador a 0")
                    with trade_lock:
                        trades_abiertos = 0
        except Exception as e:
            print(f"⚠️ [SYNC] Error sincronizando posiciones: {e}")
            # En caso de error, si llevamos mucho tiempo bloqueados, resetear
            if trades_abiertos >= MAX_TRADES_ABIERTOS:
                print(f"⚠️ [SYNC] Reset preventivo de trades_abiertos por error")
                with trade_lock:
                    trades_abiertos = 0
        time.sleep(1800)  # cada 30 minutos

# ============================================================
# MOTOR 7: BOT POLÍTICO — Polls vs Polymarket
# ============================================================

# Fuentes de polling: Wikipedia/Ballotpedia aggregators + 538-compatible APIs
# Estrategia: comparar probabilidad de encuestas con precio en Polymarket

POLITICA_KEYWORDS = [
    "election", "elect", "win", "president", "prime minister",
    "approval", "vote", "poll", "party", "senate", "congress",
    "referendum", "ballot"
]
EDGE_THRESHOLD_POLITICA = 0.15   # Gemini recomienda 12-15%, usamos 15% por seguridad
POLITICA_MIN_VOLUME = 50000      # Solo mercados con >$50k volumen (evitar slippage)
# Mercados excluidos: elecciones presidenciales USA (mercado muy eficiente)
POLITICA_EXCLUIR = ["trump", "harris", "biden", "presidential election", "us president"]

def get_simmer_divergencias():
    """Consulta el endpoint de Simmer para mercados con alta divergencia de precio"""
    try:
        url = "https://api.simmer.markets/api/sdk/opportunities"
        headers = {"Authorization": f"Bearer {SIMMER_API_KEY}"}
        r = requests.get(url, headers=headers, timeout=10, proxies={"http": PROXY_URL, "https": PROXY_URL} if PROXY_URL else None)
        print(f"🗳️ [POLITICA] Simmer status: {r.status_code} | Response: {r.text[:150]}")
        if r.status_code != 200:
            return []
        data = r.json()
        # Simmer puede devolver lista directa o dict con high_divergence
        if isinstance(data, list):
            divergencias = data
        else:
            divergencias = data.get("high_divergence", data.get("opportunities", data.get("data", [])))
        print(f"🗳️ [POLITICA] Simmer divergencias encontradas: {len(divergencias)}")
        return divergencias
    except Exception as e:
        print(f"⚠️ [POLITICA] Error obteniendo divergencias Simmer: {e}")
        return []

def get_mercados_politica():
    """Busca mercados políticos activos en Polymarket con vencimiento <= 90 días y volumen > $50k"""
    try:
        from datetime import datetime, timezone, timedelta
        limite = datetime.now(timezone.utc) + timedelta(days=90)
        url = "https://gamma-api.polymarket.com/markets?active=true&closed=false&limit=100&order=volume&ascending=false"
        r = requests.get(url, timeout=10)
        mercados = r.json() if isinstance(r.json(), list) else []
        resultado = []
        for m in mercados:
            pregunta = m.get("question", "").lower()

            # Filtro: debe tener keyword político
            if not any(kw in pregunta for kw in POLITICA_KEYWORDS):
                continue

            # Filtro: excluir mercados de elecciones USA (muy eficientes, Gemini lo recomienda)
            if any(ex in pregunta for ex in POLITICA_EXCLUIR):
                print(f"🗳️ [POLITICA] Skip USA presidencial: {pregunta[:60]}")
                continue

            # Filtro: volumen mínimo $50k para evitar slippage
            volumen = float(m.get("volume", 0) or m.get("volumeNum", 0) or 0)
            if volumen < POLITICA_MIN_VOLUME:
                continue

            # Filtro: vencimiento <= 90 días
            end_str = m.get("endDate") or m.get("end_date_iso") or m.get("endDateIso")
            if end_str:
                try:
                    end_dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
                    if end_dt > limite:
                        continue
                except:
                    pass
            resultado.append(m)
        return resultado
    except Exception as e:
        print(f"⚠️ [POLITICA] Error obteniendo mercados: {e}")
        return []

def get_prob_politica_wikipedia(pregunta):
    """
    Consulta Wikipedia para obtener datos de aprobación/encuestas.
    Estrategia simple: busca artículos de aprobación presidencial o encuestas electorales.
    Retorna probabilidad estimada o None si no encuentra datos útiles.
    """
    try:
        # Extraer entidades clave de la pregunta
        pregunta_lower = pregunta.lower()

        # Detectar tipo de mercado
        es_aprobacion = "approval" in pregunta_lower
        es_eleccion = any(w in pregunta_lower for w in ["win", "elect", "president", "prime minister"])

        # Buscar en Wikipedia API
        search_term = None
        if "trump" in pregunta_lower:
            search_term = "Donald Trump job approval rating"
        elif "biden" in pregunta_lower:
            search_term = "Joe Biden job approval rating"
        elif "macron" in pregunta_lower:
            search_term = "Emmanuel Macron approval rating"
        elif "milei" in pregunta_lower:
            search_term = "Javier Milei approval rating"
        else:
            # Extraer nombre propio (primera palabra en mayúscula)
            words = pregunta.split()
            nombres = [w for w in words if w[0].isupper() and len(w) > 3]
            if nombres:
                search_term = f"{nombres[0]} approval rating poll"

        if not search_term:
            return None

        # Wikipedia search API
        url = f"https://en.wikipedia.org/w/api.php?action=query&list=search&srsearch={requests.utils.quote(search_term)}&format=json&srlimit=1"
        r = requests.get(url, timeout=8)
        data = r.json()
        results = data.get("query", {}).get("search", [])
        if not results:
            return None

        # Por ahora retorna None — la lógica real requiere parsear el artículo
        # En la próxima iteración integramos 538 API o PredictIt
        return None

    except Exception as e:
        print(f"⚠️ [POLITICA] Error Wikipedia: {e}")
        return None

def get_prob_politica_polymarket_history(market_id):
    """
    Estrategia alternativa: usar el historial de precios del mismo mercado.
    Si el precio cayó mucho en los últimos días, puede haber reversión a la media.
    Retorna (prob_estimada, razon) o None.
    """
    try:
        url = f"https://clob.polymarket.com/prices-history?market={market_id}&interval=1d&fidelity=1"
        r = requests.get(url, timeout=8)
        data = r.json()
        history = data.get("history", [])
        if len(history) < 5:
            return None

        precios = [float(h["p"]) for h in history[-7:]]  # últimos 7 días
        precio_actual = precios[-1]
        precio_hace_7d = precios[0]
        promedio = sum(precios) / len(precios)

        # Si el precio cayó > 20% en 7 días, puede ser oversold → señal YES
        caida = precio_hace_7d - precio_actual
        if caida > 0.20 and precio_actual < 0.40:
            return (promedio, f"reversión media (cayó {caida:.2f} en 7d)")

        # Si subió > 20% en 7 días, puede ser overbought → señal NO
        subida = precio_actual - precio_hace_7d
        if subida > 0.20 and precio_actual > 0.60:
            return (promedio, f"corrección posible (subió {subida:.2f} en 7d)")

        return None
    except Exception as e:
        print(f"⚠️ [POLITICA] Error history: {e}")
        return None

mercados_politica_apostados = set()

def motor_politica():
    print("🗳️ [POLITICA] Motor político iniciado")
    time.sleep(120)  # arrancar después de los otros motores
    while True:
        try:
            # Gemini recomienda no operar política con < $5 (bloquea capital semanas)
            if saldo_wallet < 5.0:
                print(f"⏸️ [POLITICA] Saldo ${saldo_wallet:.2f} < $5 — motor pausado hasta tener más capital")
                time.sleep(1800)
                continue
            # === FUENTE 1: Divergencias IA de Simmer ===
            divergencias = get_simmer_divergencias()
            for div in divergencias:
                market_id = div.get("market_id") or div.get("id")
                if not market_id or market_id in mercados_politica_apostados:
                    continue

                pregunta_div = div.get("question", "").lower()
                if not any(kw in pregunta_div for kw in POLITICA_KEYWORDS):
                    continue
                if any(ex in pregunta_div for ex in POLITICA_EXCLUIR):
                    continue

                simmer_price = float(div.get("simmer_price", 0) or 0)
                external_price = float(div.get("external_price", 0) or 0)
                freshness = div.get("signal_freshness", "stale")

                if freshness == "stale" or simmer_price <= 0 or external_price <= 0:
                    continue

                edge = simmer_price - external_price
                print(f"🗳️ [POLITICA] Simmer: {pregunta_div[:50]} | AI: {simmer_price:.2f} | Market: {external_price:.2f} | Edge: {edge:.2f} | {freshness}")

                if abs(edge) >= EDGE_THRESHOLD_POLITICA:
                    side = "yes" if edge > 0 else "no"
                    registrar_senal("politica", pregunta_div, side, abs(edge))
                    print(f"🎯 [POLITICA] Simmer edge! {side.upper()} | {pregunta_div[:60]}")
                    ok = ejecutar_trade(
                        market_id=market_id,
                        side=side,
                        razon=f"Bot politica | Simmer divergencia ({freshness})",
                        precio_ref=external_price if side == "yes" else (1 - external_price),
                    )
                    if ok:
                        mercados_politica_apostados.add(market_id)
                time.sleep(1)

            # === FUENTE 2: Mean reversion por historial de precio ===
            mercados = get_mercados_politica()
            print(f"🗳️ [POLITICA] {len(mercados)} mercados políticos (volumen>$50k, no USA presidencial)")

            for mercado in mercados:
                market_id = mercado.get("conditionId") or mercado.get("id")
                if not market_id or market_id in mercados_politica_apostados:
                    continue

                pregunta = mercado.get("question", "")
                precio_yes = get_precio_yes(mercado)
                if not precio_yes or precio_yes <= 0.05 or precio_yes >= 0.95:
                    continue

                resultado_history = get_prob_politica_polymarket_history(market_id)
                if resultado_history:
                    prob_estimada, razon = resultado_history
                    edge = prob_estimada - precio_yes
                    print(f"🗳️ [POLITICA] MR: {pregunta[:60]} | Est: {prob_estimada:.2f} | Poly: {precio_yes:.2f} | Edge: {edge:.2f}")

                    if abs(edge) >= EDGE_THRESHOLD_POLITICA:
                        side = "yes" if edge > 0 else "no"
                        registrar_senal("politica", pregunta, side, abs(edge))
                        print(f"🎯 [POLITICA] Mean reversion edge! {side.upper()} | {pregunta[:60]}")
                        ok = ejecutar_trade(
                            market_id=market_id,
                            side=side,
                            razon=f"Bot politica | {razon}",
                            precio_ref=precio_yes if side == "yes" else (1 - precio_yes),
                            slug=mercado.get("slug")
                        )
                        if ok:
                            mercados_politica_apostados.add(market_id)

                time.sleep(2)

            time.sleep(1800)  # cada 30 minutos

        except Exception as e:
            print(f"❌ [POLITICA] Error general: {e}")
            time.sleep(60)

# ============================================================
# MOTOR 6: REPORTE DIARIO + HEARTBEAT
# ============================================================
def get_trade_journal():
    """Consulta el historial real de trades desde Simmer API"""
    try:
        url = "https://api.simmer.markets/api/sdk/trades"
        headers = {"Authorization": f"Bearer {SIMMER_API_KEY}"}
        proxies = {"http": PROXY_URL, "https": PROXY_URL} if PROXY_URL else None
        r = requests.get(url, headers=headers, timeout=10, proxies=proxies)
        data = r.json()
        trades = data if isinstance(data, list) else data.get("trades", data.get("data", []))
        return trades
    except Exception as e:
        print(f"⚠️ [JOURNAL] Error obteniendo trades: {e}")
        return []

def get_win_rate_por_motor(trades):
    """Calcula win rate por motor/source a partir del journal de Simmer"""
    stats = {}
    for t in trades:
        source = t.get("source", "unknown")
        outcome = t.get("outcome")  # "win", "loss", "pending"
        if source not in stats:
            stats[source] = {"wins": 0, "losses": 0, "pending": 0, "pnl": 0.0}
        if outcome == "win":
            stats[source]["wins"] += 1
            stats[source]["pnl"] += float(t.get("pnl", 0) or 0)
        elif outcome == "loss":
            stats[source]["losses"] += 1
            stats[source]["pnl"] += float(t.get("pnl", 0) or 0)
        else:
            stats[source]["pending"] += 1
    return stats

def motor_reporte():
    """Cada hora envía saldo al Telegram. A las 23:00 UTC manda reporte del día."""
    global senales_del_dia, trades_del_dia
    print("📊 [REPORTE] Motor de reporte iniciado")
    ultimo_dia_reportado = None

    while True:
        try:
            now = datetime.now(timezone.utc)

            # === HEARTBEAT CADA HORA ===
            actualizar_saldo_inicial()  # actualiza SALDO_INICIAL con el saldo real
            saldo = get_saldo_wallet()
            saldo_txt = f"${saldo}" if saldo is not None else "N/D"
            heartbeat = (
                f"💓 Bot activo — {now.strftime('%H:%M')} UTC\n"
                f"💰 Saldo wallet: {saldo_txt} USDC.e\n"
                f"📈 Trades abiertos: {trades_abiertos}/{MAX_TRADES_ABIERTOS}\n"
                f"🎯 Señales hoy: {len(senales_del_dia)} | Trades hoy: {len(trades_del_dia)}"
            )
            print(f"📊 [REPORTE] {heartbeat.replace(chr(10), ' | ')}")
            notify(heartbeat)

            # === REPORTE DIARIO A LAS 23:00 UTC ===
            if now.hour == 23 and ultimo_dia_reportado != now.date():
                ultimo_dia_reportado = now.date()

                with reporte_lock:
                    s_copy = list(senales_del_dia)
                    t_copy = list(trades_del_dia)
                    senales_del_dia.clear()
                    trades_del_dia.clear()

                # Agrupar señales por motor
                por_motor = {}
                for s in s_copy:
                    por_motor.setdefault(s["motor"], []).append(s)

                resumen_senales = ""
                for motor, lista in por_motor.items():
                    emoji = {"clima": "🌦️", "crypto": "₿", "deportes": "⚽", "copy": "🔁", "politica": "🗳️"}.get(motor, "📌")
                    resumen_senales += f"{emoji} {motor.upper()}: {len(lista)} señales\n"
                    for s in lista[:3]:  # máximo 3 ejemplos por motor
                        resumen_senales += f"   {s['hora']} | {s['side'].upper()} | edge {s['edge']} | {s['mercado'][:40]}\n"
                    if len(lista) > 3:
                        resumen_senales += f"   ... y {len(lista)-3} más\n"

                trades_ok = [t for t in t_copy if t["ok"]]
                trades_fail = [t for t in t_copy if not t["ok"]]

                # Trade journal desde Simmer (datos reales con outcomes)
                journal = get_trade_journal()
                win_stats = get_win_rate_por_motor(journal)
                journal_txt = ""
                if win_stats:
                    journal_txt = "\n📓 WIN RATE POR MOTOR (histórico):\n"
                    for motor, s in win_stats.items():
                        total = s["wins"] + s["losses"]
                        wr = s["wins"] / total * 100 if total > 0 else 0
                        journal_txt += f"  {motor}: {wr:.0f}% ({s['wins']}W/{s['losses']}L) | P&L: ${s['pnl']:.2f}\n"

                reporte = (
                    f"📊 REPORTE DIARIO — {now.strftime('%d/%m/%Y')}\n"
                    f"{'='*30}\n"
                    f"💰 Saldo wallet: {saldo_txt} USDC.e\n"
                    f"\n"
                    f"🎯 SEÑALES DETECTADAS: {len(s_copy)}\n"
                    f"{resumen_senales}"
                    f"\n"
                    f"✅ Trades ejecutados: {len(trades_ok)}\n"
                    f"❌ Trades fallidos: {len(trades_fail)}\n"
                    f"{journal_txt}"
                )
                if trades_ok:
                    reporte += "\nDetalle trades OK:\n"
                    for t in trades_ok:
                        reporte += f"  {t['hora']} {t['motor']} | {t['side'].upper()} ${t['monto']} | {t['mercado'][:35]}\n"

                print(f"📊 [REPORTE] Enviando reporte diario")
                notify(reporte)

            time.sleep(3600)  # esperar 1 hora

        except Exception as e:
            print(f"⚠️ [REPORTE] Error: {e}")
            time.sleep(3600)

# ============================================================
# MAIN
# ============================================================
print("🤖 Bot iniciado con 7 motores en paralelo")
print(f"👀 Copy trading: {len(TRADERS)} traders | Rango precio: {COPY_MIN_PRICE}-{COPY_MAX_PRICE}")
print(f"🌦️ Bot climático: Open-Meteo | Edge mínimo: {EDGE_THRESHOLD*100:.0f}%")
print(f"₿  Bot crypto: CoinGecko + Binance | Edge mínimo: {EDGE_THRESHOLD_CRYPTO*100:.0f}%")
print(f"⚽ Bot deportes: The Odds API | Edge mínimo: {EDGE_THRESHOLD_SPORTS*100:.0f}%")
print(f"💰 Stake fijo: ${STAKE}")
notify(
    f"🤖 Bot iniciado con 7 motores!\n"
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
t5 = threading.Thread(target=motor_sincronizacion, daemon=True)
t6 = threading.Thread(target=motor_reporte, daemon=True)
t7 = threading.Thread(target=motor_politica, daemon=True)

t1.start()
t2.start()
t3.start()
t4.start()
t5.start()
t6.start()
t7.start()

while True:
    time.sleep(60)
