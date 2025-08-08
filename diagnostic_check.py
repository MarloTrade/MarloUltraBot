import os, time, math
from dotenv import load_dotenv
load_dotenv()

from kucoin.client import User, Market, Trade

API_KEY = os.getenv("KUCOIN_API_KEY")
API_SECRET = os.getenv("KUCOIN_API_SECRET")
API_PASS = os.getenv("KUCOIN_API_PASSPHRASE")
USE_SANDBOX = os.getenv("KUCOIN_SANDBOX", "false").lower() == "true"
DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"
SYMBOL = os.getenv("TEST_SYMBOL", "BTC-USDT")
TEST_USDT = float(os.getenv("TEST_USDT", "5"))

def fail(msg):
    print(f"❌ {msg}"); raise SystemExit(1)

def ok(msg):
    print(f"✅ {msg}")

if not all([API_KEY, API_SECRET, API_PASS]):
    fail("Variables d’environnement manquantes (KUCOIN_API_KEY/SECRET/PASSPHRASE).")

user = User(API_KEY, API_SECRET, API_PASS, is_sandbox=USE_SANDBOX)
market = Market(is_sandbox=USE_SANDBOX)
trade = Trade(API_KEY, API_SECRET, API_PASS, is_sandbox=USE_SANDBOX)

srv_time = market.get_server_time()
drift_ms = abs(int(srv_time) - int(time.time()*1000))
ok(f"Ping OK. Time drift ~ {drift_ms} ms")

accounts = user.get_account_list()
by_type = {}
for a in accounts: by_type.setdefault(a['type'], []).append(a)
def balance(typ, currency):
    return sum(float(a['balance']) for a in by_type.get(typ, []) if a['currency']==currency)

usdt_trade = balance('trade', 'USDT'); usdt_main = balance('main', 'USDT')
ok(f"Balances: trade USDT={usdt_trade}, main USDT={usdt_main}")
if usdt_trade < 1e-6 and usdt_main > 0:
    print("ℹ️ Transfère des USDT du Main vers le Trade account pour pouvoir trader.")

symbols = {s['symbol']: s for s in market.get_symbol_list()}
if SYMBOL not in symbols: fail(f"Symbole {SYMBOL} introuvable.")
info = symbols[SYMBOL]
base, quote = info['baseCurrency'], info['quoteCurrency']
price_tick = float(info['priceIncrement']); size_step = float(info['baseIncrement'])
min_funds  = float(info.get('minFunds', '0')) or 0.0
ok(f"{SYMBOL} -> base={base} quote={quote}, tick={price_tick}, step={size_step}, minFunds={min_funds}")

ticker = market.get_ticker(SYMBOL)
best_bid = float(ticker['bestBid'])
test_quote = max(TEST_USDT, min_funds if min_funds>0 else TEST_USDT)
qty = test_quote / best_bid
qty = math.floor(qty / size_step) * size_step
if qty <= 0: fail("Quantité calculée <= 0 (augmente TEST_USDT ou choisis une autre paire).")
ok(f"Prix ~ {best_bid}, qty test={qty}")

if DRY_RUN:
    ok("DRY_RUN=TRUE → Aucun ordre réel envoyé.")
    print("➡️ Le bot PEUT trader. Pour tester en réel, passe DRY_RUN=false.")
else:
    price = round((best_bid * 0.995) / price_tick) * price_tick
    try:
        res = trade.create_limit_order(SYMBOL, 'buy', str(qty), str(price))
        ok(f"Ordre LIMIT BUY envoyé: id={res.get('orderId') or res}")
        if 'orderId' in res:
            trade.cancel_order(res['orderId']); ok("Ordre annulé (test concluant).")
        else:
            ok("Réponse inattendue mais requête passée.")
    except Exception as e:
        fail(f"Echec placement ordre: {e}")

print("\n🎯 DIAG TERMINÉ: si tout est vert, le blocage vient des CONDITIONS STRATÉGIQUES.")