# main.py — MarloTrader (smart mode)
import time, traceback
from collections import defaultdict
from logger_setup import setup_logger
from config import CFG
from exchange import Ku
from strategy import ema_cross_signal
from telegram_alerts import send_alert

logger = setup_logger()

# Mémoire simple (perdue au redémarrage)
# positions[symbol] = {"entry": float, "size": float}
positions = {}
cooldown = defaultdict(float)  # symbol -> next_allowed_ts

# ---------- Utils marché / sizing ----------
def calc_order_size_quote(free_quote):
    """Montant à engager dans la monnaie de cotation (USDT/BTC/...)."""
    rp = max(1.0, min(CFG["RISK_PCT"], 100.0))
    size = free_quote * (rp / 100.0)
    return max(size, CFG["MIN_TRADE_USDT"])

def ensure_qty(exchange, symbol, quote_amount):
    """Calcule une quantité respectant les increments et minFunds."""
    smap = exchange.symbols_map()[symbol]
    price_tick = float(smap['priceIncrement'])
    size_step  = float(smap['baseIncrement'])
    min_funds  = float(smap.get('minFunds', '0') or 0)
    bid        = float(exchange.ticker(symbol)['bestBid'])
    quote      = max(quote_amount, min_funds if min_funds > 0 else quote_amount)
    qty        = quote / bid
    qty        = exchange.snap_qty(qty, size_step)
    return max(qty, 0.0), bid, price_tick

def atr_pct(kl, period=14):
    """ATR% simple sur bougies KuCoin (15m par défaut ailleurs)."""
    if len(kl) < period + 1:
        return 0.0
    trs = []
    for i in range(1, len(kl)):
        # kline KuCoin: [time, open, close, high, low, volume, turnover]
        _, o, c, h, l, *_ = kl[i]
        prev_c = float(kl[i-1][2])
        h, l, c = float(h), float(l), float(c)
        tr = max(h - l, abs(h - prev_c), abs(l - prev_c))
        trs.append(tr)
    avg_tr = sum(trs[-period:]) / period
    last_close = float(kl[-1][2])
    return (avg_tr / last_close) * 100 if last_close > 0 else 0.0

def free_after_reserve(quote, free_quote):
    """Garde une petite réserve pour éviter le blocage complet."""
    if quote == "USDT":
        return max(0.0, free_quote - CFG.get("RESERVE_USDT", 20.0))
    if quote == "BTC":
        return max(0.0, free_quote - CFG.get("RESERVE_BTC", 0.0002))
    return free_quote

def allocation_pct(ex, base, quote):
    """Part approximative déjà allouée à 'base' (en % du capital sur cette quote)."""
    base_bal = ex.balance('trade', base)
    quote_free = ex.balance('trade', quote)
    pair = f"{base}-{quote}"
    price = float(ex.ticker(pair)['price']) if pair in ex.symbols_map() else 0.0
    pos_val = base_bal * price
    total = pos_val + quote_free
    return (pos_val / total * 100) if total > 0 else 0.0

# ---------- TP / SL (gérés côté bot) ----------
def maybe_place_tp_sl(symbol, entry_price):
    if not CFG["ENABLE_TP_SL"]:
        return
    tp = entry_price * (1 + CFG["TP_PCT"]/100.0)
    sl = entry_price * (1 - CFG["SL_PCT"]/100.0)
    logger.info(f"{symbol} TP/SL armés → TP≈{tp:.6f} (+{CFG['TP_PCT']}%), SL≈{sl:.6f} (-{CFG['SL_PCT']}%)")
    send_alert(f"{symbol} TP/SL armés → TP≈{tp:.6f} / SL≈{sl:.6f}")

def check_positions_for_tp_sl(ex, symbols_to_check):
    if not positions or not CFG["ENABLE_TP_SL"]:
        return
    smap = ex.symbols_map()
    for symbol in symbols_to_check:
        if symbol not in positions or symbol not in smap:
            continue
        entry = positions[symbol]["entry"]
        size  = positions[symbol]["size"]
        last  = float(ex.ticker(symbol)['price'])
        tp = entry * (1 + CFG["TP_PCT"]/100.0)
        sl = entry * (1 - CFG["SL_PCT"]/100.0)
        if last >= tp:
            res = ex.place_order(symbol, "sell", size=str(size), type_="market")
            logger.info(f"{symbol} SELL TP -> {res}")
            send_alert(f"TP atteint ✅ {symbol} ~{last:.6f}")
            positions.pop(symbol, None)
        elif last <= sl:
            res = ex.place_order(symbol, "sell", size=str(size), type_="market")
            logger.info(f"{symbol} SELL SL -> {res}")
            send_alert(f"SL déclenché ❌ {symbol} ~{last:.6f}")
            positions.pop(symbol, None)

# ---------- Smart router (conversion automatique multi-quotes) ----------
def best_price(ex, pair, side):
    t = ex.ticker(pair)
    return float(t['bestAsk']) if side == "buy" else float(t['bestBid'])

def route_to_quote(ex, target_quote: str, min_needed: float):
    """
    Assure 'target_quote' dispo en convertissant depuis une autre quote listée
    dans CFG['QUOTES'] via un pont direct (ex: BTC<->USDT).
    """
    if min_needed <= 0:
        return
    smap = ex.symbols_map()
    quotes = CFG["QUOTES"]

    for q in quotes:
        if q == target_quote:
            continue
        bal = ex.balance('trade', q)
        if bal <= 0:
            continue

        # q -> target_quote
        direct1 = f"{q}-{target_quote}"      # vendre q contre target_quote
        direct2 = f"{target_quote}-{q}"      # acheter target_quote en payant q

        if direct1 in smap:
            px = best_price(ex, direct1, side="sell")
            need_q = min_needed / px if px > 0 else 0
            size = min(bal, need_q)
            if size > 0:
                logger.info(f"[Router] {q}→{target_quote} via {direct1} (sell {size} {q})")
                ex.place_order(direct1, "sell", size=str(size), type_="market")
                return

        if direct2 in smap:
            px = best_price(ex, direct2, side="buy")
            size_base = min_needed / px if px > 0 else 0  # BASE = target_quote
            if size_base > 0:
                affordable = bal / px if px > 0 else 0
                size = min(size_base, affordable)
                if size > 0:
                    logger.info(f"[Router] {q}→{target_quote} via {direct2} (buy {size} {target_quote})")
                    ex.place_order(direct2, "buy", size=str(size), type_="market")
                    return

    logger.info(f"[Router] Pas de route simple vers {target_quote} ou soldes insuffisants.")

# ---------- Boucle principale ----------
def run_loop():
    logger.info(f"Config: {CFG}")
    ex = Ku(logger)
    drift = ex.time_ok()
    if drift > 15000:
        logger.warning("Time drift élevé, pense à resynchroniser l'horloge du serveur.")

    while True:
        try:
            smap = ex.symbols_map()
            now = time.time()

            for quote in CFG["QUOTES"]:
                # Solde libre avec réserve
                free_q = ex.balance('trade', quote)
                free_q = free_after_reserve(quote, free_q)
                logger.info(f"[{quote}] balance libre (après réserve): {free_q}")

                # Symboles correspondant à cette quote
                symbols_for_quote = [s for s in CFG["SYMBOLS"] if s.endswith(f"-{quote}")]

                # Gestion TP/SL en priorité
                check_positions_for_tp_sl(ex, symbols_for_quote)

                for symbol in symbols_for_quote:
                    # Pair fallback auto (si la paire n'existe pas, on tente une autre quote)
                    if symbol not in smap:
                        base, _ = symbol.split('-')
                        fallback = None
                        for q2 in CFG["QUOTES"]:
                            alt = f"{base}-{q2}"
                            if alt in smap:
                                fallback = alt
                                break
                        if not fallback:
                            logger.warning(f"{symbol} introuvable, aucune alternative trouvée.")
                            continue
                        logger.info(f"{symbol} introuvable → fallback sur {fallback}")
                        symbol = fallback

                    if now < cooldown[symbol]:
                        continue

                    base, quote_curr = symbol.split('-')

                    # Si on démarre avec un solde base déjà présent, on enregistre la position
                    base_bal = ex.balance('trade', base)
                    if base_bal > 0 and symbol not in positions:
                        last = float(ex.ticker(symbol)['price'])
                        positions[symbol] = {"entry": last, "size": base_bal}
                        logger.info(f"{symbol} position détectée → entry≈{last:.6f}, size={base_bal}")
                        maybe_place_tp_sl(symbol, last)

                    # Données marché + signal
                    kl = ex.klines(symbol, "15min", limit=120)
                    vol = atr_pct(kl, period=14)
                    if vol < CFG.get("MIN_ATR_PCT", 0.3):
                        logger.info(f"{symbol} volatilité faible ({vol:.2f}% ATR), skip.")
                        continue

                    closes = [float(x[2]) for x in kl]
                    sig, reason = ema_cross_signal(closes)
                    logger.info(f"{symbol} signal={sig} ({reason})")

                    # SELL si signal + position
                    if sig == "sell":
                        base_bal = ex.balance('trade', base)
                        if base_bal > 0:
                            res = ex.place_order(symbol, "sell", size=str(base_bal), type_="market")
                            logger.info(f"{symbol} SELL market -> {res}")
                            send_alert(f"SELL {symbol} size={base_bal} (raison: {reason})")
                            positions.pop(symbol, None)
                            cooldown[symbol] = now + CFG.get("COOLDOWN_SEC", 90)
                        continue

                    # BUY si signal
                    if sig == "buy":
                        # anti-empilement
                        if ex.balance('trade', base) > 0 or symbol in positions:
                            logger.info(f"{symbol} déjà en position, skip buy.")
                            continue

                        # allocation max par actif
                        alloc = allocation_pct(ex, base, quote_curr)
                        if alloc >= CFG.get("MAX_POS_ALLOCATION_PCT", 50.0):
                            logger.info(f"{symbol} allocation {alloc:.1f}% >= max {CFG['MAX_POS_ALLOCATION_PCT']}%, skip.")
                            continue

                        # s'il manque de quote, on route automatiquement (ex: manque d'USDT, on vend un peu de BTC -> USDT)
                        free_here = free_after_reserve(quote_curr, ex.balance('trade', quote_curr))
                        if free_here < CFG["MIN_TRADE_USDT"]:
                            need = CFG["MIN_TRADE_USDT"] - free_here
                            route_to_quote(ex, quote_curr, need)
                            free_here = free_after_reserve(quote_curr, ex.balance('trade', quote_curr))
                            if free_here < CFG["MIN_TRADE_USDT"]:
                                logger.info(f"{symbol} pas assez de {quote_curr} après routage, skip.")
                                continue

                        quote_amt = calc_order_size_quote(free_here)
                        qty, bid, _ = ensure_qty(ex, symbol, quote_amt)
                        if qty <= 0:
                            logger.info(f"{symbol} qty<=0, skip")
                            continue

                        res = ex.place_order(symbol, "buy", size=str(qty), type_="market")
                        logger.info(f"{symbol} BUY market -> {res}")
                        send_alert(f"BUY {symbol} qty={qty} (raison: {reason})")

                        entry = float(ex.ticker(symbol)['price'])
                        positions[symbol] = {"entry": entry, "size": qty}
                        maybe_place_tp_sl(symbol, entry)
                        cooldown[symbol] = now + CFG.get("COOLDOWN_SEC", 90)

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