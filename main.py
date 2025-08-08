import time, traceback
from logger_setup import setup_logger
from config import CFG
from exchange import Ku
from strategy import ema_cross_signal
from telegram_alerts import send_alert

logger = setup_logger()

# Mémoire simple des positions ouvertes (perdues au redémarrage, suffisant pour démarrer propre)
# positions[symbol] = {"entry": float, "size": float}
positions = {}

def calc_order_size_quote(free_quote):
    """Montant à engager dans la monnaie de cotation (USDT/BTC/...)."""
    rp = max(1.0, min(CFG["RISK_PCT"], 100.0))
    size = free_quote * (rp / 100.0)
    return max(size, CFG["MIN_TRADE_USDT"])

def ensure_qty(exchange, symbol, quote_amount):
    """Calcule une quantité respectant les incréments/minFunds."""
    smap = exchange.symbols_map()[symbol]
    price_tick = float(smap['priceIncrement'])
    size_step = float(smap['baseIncrement'])
    min_funds = float(smap.get('minFunds', '0') or 0)
    bid = float(exchange.ticker(symbol)['bestBid'])
    quote = max(quote_amount, min_funds if min_funds > 0 else quote_amount)
    qty = quote / bid
    qty = exchange.snap_qty(qty, size_step)
    return max(qty, 0.0), bid, price_tick

def maybe_place_tp_sl(symbol, entry_price):
    """Annonce et log des objectifs; l’exécution réelle est gérée par le loop (surveillance prix)."""
    if not CFG["ENABLE_TP_SL"]: 
        return
    tp = entry_price * (1 + CFG["TP_PCT"]/100.0)
    sl = entry_price * (1 - CFG["SL_PCT"]/100.0)
    logger.info(f"{symbol} TP/SL armés → TP≈{tp:.6f} (+{CFG['TP_PCT']}%), SL≈{sl:.6f} (-{CFG['SL_PCT']}%)")
    send_alert(f"{symbol} TP/SL armés → TP≈{tp:.6f} / SL≈{sl:.6f}")

def check_positions_for_tp_sl(ex, symbols_to_check):
    """Surveille le prix et vend au marché si TP ou SL atteints."""
    if not positions or not CFG["ENABLE_TP_SL"]:
        return
    smap = ex.symbols_map()
    for symbol in symbols_to_check:
        if symbol not in positions:
            continue
        if symbol not in smap:
            continue
        entry = positions[symbol]["entry"]
        size  = positions[symbol]["size"]
        last  = float(ex.ticker(symbol)['price'])  # dernier prix
        tp = entry * (1 + CFG["TP_PCT"]/100.0)
        sl = entry * (1 - CFG["SL_PCT"]/100.0)

        if last >= tp:
            # Take Profit
            res = ex.place_order(symbol, "sell", size=str(size), type_="market")
            logger.info(f"{symbol} SELL TP -> {res}")
            send_alert(f"TP atteint ✅ {symbol} @ ~{last:.6f}")
            positions.pop(symbol, None)
        elif last <= sl:
            # Stop Loss
            res = ex.place_order(symbol, "sell", size=str(size), type_="market")
            logger.info(f"{symbol} SELL SL -> {res}")
            send_alert(f"SL déclenché ❌ {symbol} @ ~{last:.6f}")
            positions.pop(symbol, None)

def run_loop():
    logger.info(f"Config: {CFG}")
    ex = Ku(logger)
    drift = ex.time_ok()
    if drift > 15000:
        logger.warning("Time drift élevé, pense à resynchroniser l'horloge du serveur.")

    # Boucle principale
    while True:
        try:
            smap = ex.symbols_map()
            # Pour chaque quote (USDT, BTC, ...)
            for quote in CFG["QUOTES"]:
                free_quote = ex.balance('trade', quote)
                logger.info(f"[{quote}] balance libre: {free_quote}")

                # Filtrer les symboles qui matchent cette quote
                symbols_for_quote = [s for s in CFG["SYMBOLS"] if s.endswith(f"-{quote}")]
                # Surveillance TP/SL d'abord (priorité à la gestion du risque)
                check_positions_for_tp_sl(ex, symbols_for_quote)

                for symbol in symbols_for_quote:
                    if symbol not in smap:
                        logger.warning(f"{symbol} introuvable, skip.")
                        continue

                    base = symbol.split('-')[0]
                    base_bal = ex.balance('trade', base)

                    # Anti-empilement : si on détient déjà du base, on évite de racheter
                    if base_bal > 0 and symbol not in positions:
                        # On initialise une position si le bot démarre avec un solde existant
                        last = float(ex.ticker(symbol)['price'])
                        positions[symbol] = {"entry": last, "size": base_bal}
                        logger.info(f"{symbol} position détectée (démarrage) → entry≈{last:.6f}, size={base_bal}")
                        maybe_place_tp_sl(symbol, last)

                    # Génère signal via stratégie
                    closes = [float(x[2]) for x in ex.klines(symbol, "15min", limit=120)]
                    sig, reason = ema_cross_signal(closes)
                    logger.info(f"{symbol} signal={sig} ({reason})")

                    if sig == "buy":
                        if base_bal > 0:
                            logger.info(f"{symbol} déjà en position ({base} balance: {base_bal}), skip buy.")
                            continue
                        if free_quote < CFG["MIN_TRADE_USDT"]:
                            logger.info(f"{symbol} pas assez de {quote} ({free_quote} < {CFG['MIN_TRADE_USDT']})")
                            continue

                        quote_amt = calc_order_size_quote(free_quote)
                        qty, bid, _ = ensure_qty(ex, symbol, quote_amt)
                        if qty <= 0:
                            logger.info(f"{symbol} qty<=0, skip")
                            continue

                        res = ex.place_order(symbol, "buy", size=str(qty), type_="market")
                        logger.info(f"{symbol} BUY market -> {res}")
                        send_alert(f"BUY {symbol} qty={qty} (raison: {reason})")

                        # Enregistre position
                        entry = float(ex.ticker(symbol)['price'])
                        positions[symbol] = {"entry": entry, "size": qty}
                        maybe_place_tp_sl(symbol, entry)

                    elif sig == "sell":
                        if base_bal <= 0 and symbol not in positions:
                            logger.info(f"{symbol} aucun solde {base} à vendre")
                            continue
                        size_to_sell = base_bal if base_bal > 0 else positions[symbol]["size"]
                        res = ex.place_order(symbol, "sell", size=str(size_to_sell), type_="market")
                        logger.info(f"{symbol} SELL market -> {res}")
                        send_alert(f"SELL {symbol} size={size_to_sell} (raison: {reason})")
                        positions.pop(symbol, None)

            time.sleep(CFG["POLL_INTERVAL_SEC"])

        except KeyboardInterrupt:
            logger.info("Arrêt manuel.")
            break
        except Exception as e:
            logger.error(f"Loop error: {e}\n{traceback.format_exc()}")
            send_alert(f"Erreur loop: {e}")
            time.sleep(5)

if __name__ == "__main__":
    run_loop()