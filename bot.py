import os
import time
import requests
from simmer_sdk import SimmerClient

# === CONFIG ===
SIMMER_API_KEY = os.environ.get("SIMMER_API_KEY")
WALLET_PRIVATE_KEY = os.environ.get("WALLET_PRIVATE_KEY")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
MAX_USD = float(os.environ.get("MAX_USD", "2"))

# Wallets a copiar (separadas por coma en la variable de Railway)
raw_wallets = os.environ.get("SIMMER_COPYTRADING_WALLETS", "")
TRADERS = [w.strip() for w in raw_wallets.split(",") if w.strip()]

# === TELEGRAM ===
def notify(msg):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": msg}, timeout=10)
    except Exception as e:
        print(f"⚠️ Telegram error: {e}")

# === SIMMER CLIENT ===
# Simmer maneja el geoblock internamente — no necesitamos proxy
# El SDK espera la key sin 0x
key = WALLET_PRIVATE_KEY or ""
if key.startswith("0x"):
    key = key[2:]
os.environ["SIMMER_PRIVATE_KEY"] = key

client = SimmerClient(
    api_key=SIMMER_API_KEY,
    venue="polymarket"
)

# === MONITOR WALLETS ===
trades_copiados = set()

def get_trades_del_trader(wallet):
    url = f"https://data-api.polymarket.com/activity?user={wallet}&limit=5&type=TRADE"
    try:
        r = requests.get(url, timeout=10)
        return r.json()
    except Exception as e:
        print(f"❌ Error obteniendo trades de {wallet[:8]}: {e}")
        return []

def copiar_trade(asset, side, price):
    try:
        size = round(MAX_USD / price, 2)
        result = client.trade(
            market_id=asset,
            side=side.lower(),   # "yes" o "no"
            amount=MAX_USD
        )
        return result
    except Exception as e:
        raise e

# === MAIN LOOP ===
print("🤖 Bot de copy trading iniciado!")
print(f"👀 Copiando trades de {len(TRADERS)} traders: {[w[:8] for w in TRADERS]}")
print(f"💰 Monto max por trade: ${MAX_USD}")
notify(f"🤖 Bot iniciado. Copiando {len(TRADERS)} traders. Max ${MAX_USD}/trade.")

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
                price_raw = trade.get("price", 0)

                try:
                    price = float(price_raw)
                except:
                    continue

                if not asset or not side or price <= 0:
                    continue

                # Solo copiamos BUY (YES) por ahora
                if side not in ("BUY", "YES"):
                    trades_copiados.add(trade_id)
                    continue

                print(f"🔔 Nuevo trade de {wallet[:8]}... | Asset: {asset[:20]}... | Side: {side} | Precio: {price}")

                try:
                    result = copiar_trade(asset, "yes", price)
                    msg = f"✅ Trade copiado!\nWallet: {wallet[:8]}...\nPrecio: {price}\nMonto: ${MAX_USD}\nResultado: {result}"
                    print(msg)
                    notify(msg)
                    trades_copiados.add(trade_id)
                except Exception as e:
                    err = f"❌ Error copiando trade: {e}"
                    print(err)
                    notify(err)

        time.sleep(30)

    except Exception as e:
        print(f"❌ Error general: {e}")
        time.sleep(30)
