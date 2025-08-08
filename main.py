import time, traceback
from logger_setup import setup_logger
from config import CFG
from exchange import Ku
from strategy import ema_cross_signal
from telegram_alerts import send_alert

logger = setup_logger()

def calc_order_size_usdt(usdt_balance):
    risk_pct = max(1.0, min(CFG["RISK_PCT"], 100.0))
    size = usdt_balance * (risk_pct / 100.0)
    return max(size, CFG["MIN_TRADE_USDT"])

def ensure_qty(exchange, symbol, quote_amount_usdt):
    smap = exchange.symbols_map()[symbol]
    price_tick = float(smap['priceIncrement'])
    size_step = float(smap['baseIncrement'])
    min_funds = float(smap.get('minFunds', '0') or 0)
    bid = float(exchange.ticker(symbol)['bestBid'])
    quote = max(quote_amount_usdt, min_funds if min_funds>0 else quote_amount_usdt)
    qty = quote / bid
    qty = exchange.snap_qty(qty, size_step)
    return max(qty, 0.0), bid, price_tick

def test_trade_once(exchange, symbol):
    try:
        qty, bid, price_tick = ensure_qty(exchange, symbol, CFG["MIN_TRADE_USDT"])
        if qty <= 0:
            logger.warning(f"TEST_TRADE: qty<=0 sur {symbol}. Augmente MIN_TRADE_USDT."); return
        price = round(bid * 0.995 / price_tick) * price_tick
        res = exchange.place_order(symbol, "buy", size=str(qty), price=str(price), type_="limit")
        logger.info(f"TEST_TRADE: BUY sent {res}")
        if res and res.get("orderId") and res["orderId"] != "DRYRUN":
            exchange.cancel_order(res["orderId"]); logger.info("TEST_TRADE: order cancelled")
        send_alert(f"TEST_TRADE OK sur {symbol} (qty={qty})")
    except Exception as e:
        logger.error(f"TEST_TRADE error: {e}"); send_alert(f"TEST_TRADE ERREUR: {e}")

def run_loop():
    logger.info(f"Config: {CFG}")
    ex = Ku(logger)
    drift = ex.time_ok()
    if drift > 15000: logger.warning("Time drift élevé, pense à resynchroniser l'horloge du serveur.")
    usdt = ex.balance('trade', CFG["QUOTE"]); logger.info(f"USDT (trade): {usdt}")
    if CFG["TEST_TRADE"]: test_trade_once(ex, CFG["SYMBOLS"][0])

    while True:
        try:
            smap = ex.symbols_map()
            for symbol in CFG["SYMBOLS"]:
                if symbol not in smap: logger.warning(f"{symbol} introuvable, skip."); continue
                closes = [float(x[2]) for x in ex.klines(symbol, "15min", limit=120)]
                sig, reason = ema_cross_signal(closes)
                logger.info(f"{symbol} signal={sig} ({reason})")
                if sig == "buy":
                    usdt = ex.balance('trade', CFG["QUOTE"])
                    if usdt < CFG["MIN_TRADE_USDT"]:
                        logger.info(f"{symbol} pas assez d'USDT ({usdt}<{CFG['MIN_TRADE_USDT']})"); continue
                    quote_amt = calc_order_size_usdt(usdt)
                    qty, bid, price_tick = ensure_qty(ex, symbol, quote_amt)
                    if qty <= 0: logger.info(f"{symbol} qty calculée <=0, skip"); continue
                    res = ex.place_order(symbol, "buy", size=str(qty), type_="market")
                    logger.info(f"{symbol} BUY market -> {res}")
                    send_alert(f"BUY {symbol} qty={qty} (raison: {reason})")
                    if CFG["ENABLE_TP_SL"]:
                        logger.info(f"{symbol} TP/SL à implémenter → TP {CFG['TP_PCT']}% / SL {CFG['SL_PCT']}%")
                elif sig == "sell":
                    base = symbol.split('-')[0]
                    base_bal = ex.balance('trade', base)
                    if base_bal <= 0: logger.info(f"{symbol} aucun solde {base} à vendre"); continue
                    res = ex.place_order(symbol, "sell", size=str(base_bal), type_="market")
                    logger.info(f"{symbol} SELL market -> {res}")
                    send_alert(f"SELL {symbol} size={base_bal} (raison: {reason})")
            time.sleep(CFG["POLL_INTERVAL_SEC"])
        except KeyboardInterrupt:
            logger.info("Arrêt manuel."); break
        except Exception as e:
            logger.error(f"Loop error: {e}\n{traceback.format_exc()}"); send_alert(f"Erreur loop: {e}"); time.sleep(5)

if __name__ == "__main__":
    run_loop()