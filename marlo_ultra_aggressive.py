import time
import os
from kucoin.client import Client

API_KEY = os.getenv("KUCOIN_API_KEY")
API_SECRET = os.getenv("KUCOIN_API_SECRET")
API_PASSPHRASE = os.getenv("KUCOIN_API_PASSPHRASE")

client = Client(API_KEY, API_SECRET, API_PASSPHRASE)

TOKENS = ["WIF-USDT", "TURBO-USDT", "PEPE-USDT", "FET-USDT", "GME-USDT", "JUP-USDT", "BONK-USDT"]
INVEST_AMOUNT = 50
STOP_LOSS = -20
TAKE_PROFIT = 50
DELAY_SECONDS = 60

positions = {}

def get_price(symbol):
    try:
        return float(client.get_ticker(symbol)['price'])
    except:
        return None

def send_telegram(message):
    print(f"[TELEGRAM] {message}")

def buy_token(symbol, amount_usdt):
    price = get_price(symbol)
    if price is None:
        return
    qty = round(amount_usdt / price, 5)
    client.create_market_order(symbol, 'buy', size=str(qty))
    positions[symbol] = {'entry': price, 'qty': qty}
    send_telegram(f"✅ Achat {symbol} à {price:.4f} (qty: {qty})")

def sell_token(symbol):
    if symbol not in positions:
        return
    qty = positions[symbol]['qty']
    client.create_market_order(symbol, 'sell', size=str(qty))
    send_telegram(f"❌ Vente {symbol} (qty: {qty})")
    del positions[symbol]

def check_signal(symbol):
    price = get_price(symbol)
    if price is None:
        return
    ticker = client.get_ticker(symbol)
    volume = float(ticker.get("volValue", 0))
    if symbol not in positions and volume > 1_000_000:
        buy_token(symbol, INVEST_AMOUNT)
    if symbol in positions:
        entry = positions[symbol]['entry']
        pnl = ((price - entry) / entry) * 100
        if pnl >= TAKE_PROFIT or pnl <= STOP_LOSS:
            sell_token(symbol)

while True:
    try:
        for token in TOKENS:
            check_signal(token)
        time.sleep(DELAY_SECONDS)
    except Exception as e:
        send_telegram(f"⚠️ Erreur : {e}")
        time.sleep(120)
