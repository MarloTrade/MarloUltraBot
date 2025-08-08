# main.py — MarloTrader (smart router multi-hop)
import time, traceback
from collections import defaultdict, deque
from logger_setup import setup_logger
from config import CFG
from exchange import Ku
from strategy import ema_cross_signal
from telegram_alerts import send_alert

logger = setup_logger()

# === Mémoire positions (volatile) ===
positions = {}                # positions[symbol] = {"entry": float, "size": float}
cooldown  = defaultdict(float)  # symbol -> next_allowed_ts

# === Helpers sizing / marché ===
def calc_order_size_quote(free_quote):
    rp = max(1.0, min(CFG.get("RISK_PCT", 10.0), 100.0))
    size = free_quote * (rp / 100.0)
    return max(size, CFG.get("MIN_TRADE_USDT", 10.0))

def ensure_qty(exchange, symbol, quote_amount):
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
    if len(kl) < period + 1: return 0.0
    trs = []
    for i in range(1, len(kl)):
        _, o, c, h, l, *_ = kl[i]
        prev_c = float(kl[i-1][2])
        h, l, c = float(h), float(l), float(c)
        tr = max(h - l, abs(h - prev_c), abs(l - prev_c))
        trs.append(tr)
    avg_tr = sum(trs[-period:]) / period
    last_close = float(kl[-1][2])
    return (avg_tr / last_close) * 100 if last_close > 0 else 0.0

def free_after_reserve(quote, free_quote):
    if quote == "USDT": return max(0.0, free_quote - CFG.get("RESERVE_USDT", 20.0))
    if quote == "BTC":  return max(0.0, free_quote - CFG.get("RESERVE_BTC", 0.0002))
    return free_quote

def allocation_pct(ex, base, quote):
    base_bal   = ex.balance('trade', base)
    quote_free = ex.balance('trade', quote)
    pair = f"{base}-{quote}"
    price = float(ex.ticker(pair)['price']) if pair in ex.symbols_map() else 0.0
    pos_val = base_bal * price
    total   = pos_val + quote_free
    return (pos_val / total * 100) if total > 0 else 0.0

# === TP/SL côté bot ===
def maybe_place_tp_sl(symbol, entry):
    if not CFG.get("ENABLE_TP_SL", True): return
    tp = entry * (1 + CFG.get("TP_PCT", 1.5)/100.0)
    sl = entry * (1 - CFG.get("SL_PCT", 1.0)/100.0)
    logger.info(f"{symbol} TP/SL armés → TP≈{tp:.6f} (+{CFG.get('TP_PCT',1.5)}%), SL≈{sl:.6f} (-{CFG.get('SL_PCT',1.0)}%)")
    send_alert(f"{symbol} TP/SL armés → TP≈{tp:.6f} / SL≈{sl:.6f}")

def check_positions_for_tp_sl(ex, symbols_to_check):
    if not positions or not CFG.get("ENABLE_TP_SL", True): return
    smap = ex.symbols_map()
    for symbol in symbols_to_check:
        if symbol not in positions or symbol not in smap: continue
        entry = positions[symbol]["entry"]
        size  = positions[symbol]["size"]
        last  = float(ex.ticker(symbol)['price'])
        tp = entry * (1 + CFG.get("TP_PCT", 1.5)/100.0)
        sl = entry * (1 - CFG.get("SL_PCT", 1.0)/100.0)
        if last >= tp:
            res = ex.place_order(symbol, "sell", size=str(size), type_="market")
            logger.info(f"{symbol} SELL TP -> {res}"); send_alert(f"TP atteint ✅ {symbol} ~{last:.6f}")
            positions.pop(symbol, None)
        elif last <= sl:
            res = ex.place_order(symbol, "sell", size=str(size), type_="market")
            logger.info(f"{symbol} SELL SL -> {res}"); send_alert(f"SL déclenché ❌ {symbol} ~{last:.6f}")
            positions.pop(symbol, None)

# === Smart Router (multi-hop) ===
def best_price(ex, pair, side):
    t = ex.ticker(pair)
    return float(t['bestAsk']) if side == "buy" else float(t['bestBid'])

def quotes_graph(ex):
    """Graphe dirigé entre quotes à partir des marchés dispo (ex: BTC-USDT crée arêtes BTC->USDT et USDT->BTC)."""
    g = defaultdict(set)
    smap = ex.symbols_map()
    for sym in smap.keys():
        base, quote = sym.split('-')
        g[base].add(quote)   # on peut vendre BASE pour obtenir QUOTE
        g[quote].add(base)   # on peut acheter BASE en payant QUOTE
    return g

def find_quote_path(ex, start_q, target_q, max_hops=3):
    """BFS simple sur les quotes (USDT/BTC/...) pour trouver un chemin court."""
    if start_q == target_q: return [start_q]
    g = quotes_graph(ex)
    seen = {start_q}; dq = deque([(start_q, [start_q])])
    while dq:
        cur, path = dq.popleft()
        if len(path) > max_hops + 1: continue
        for nxt in g[cur]:
            if nxt in seen: continue
            np = path + [nxt]
            if nxt == target_q: return np
            seen.add(nxt); dq.append((nxt, np))
    return None

def execute_quote_path(ex, path, amount_in_start):
    """
    Exécute la conversion le long du chemin de quotes.
    amount_in_start: montant dispo dans la quote de départ.
    On convertit 'juste ce qu'il faut' pour atteindre la taille minimale d'achat.
    """
    if not path or len(path) == 1: return
    smap = ex.symbols_map()

    amt = amount_in_start
    for i in range(len(path)-1):
        q_from = path[i]
        q_to   = path[i+1]
        # Cherche une paire utilisable entre q_from et q_to
        pair_sell = f"{q_from}-{q_to}"   # on vend q_from (BASE) pour obtenir q_to
        pair_buy  = f"{q_to}-{q_from}"   # on achète q_to (BASE) en payant q_from

        if pair_sell in smap:
            px = best_price(ex, pair_sell, side="sell")
            size_base = amt  # taille en BASE = q_from
            if size_base <= 0: break
            logger.info(f"[Router] {q_from}→{q_to} via {pair_sell} (sell {size_base} {q_from})")
            ex.place_order(pair_sell, "sell", size=str(size_base), type_="market")
            amt = size_base * px  # reçu en q_to (approx)
        elif pair_buy in smap:
            px = best_price(ex, pair_buy, side="buy")
            size_base = amt / px if px > 0 else 0  # BASE = q_to
            if size_base <= 0: break
            logger.info(f"[Router] {q_from}→{q_to} via {pair_buy} (buy {size_base} {q_to})")
            ex.place_order(pair_buy, "buy", size=str(size_base), type_="market")
            amt = size_base  # maintenant on détient q_to en 'size_base'
        else:
            logger.info(f"[Router] Pas de marché direct entre {q_from} et {q_to}, arrêt.")
            break

# === Boucle principale ===
def run_loop():
    logger.info(f"Config: {CFG}")
    ex = Ku(logger)
    drift = ex.time_ok()
    if drift > 15000:
        logger.warning("Time drift élevé, pense à resynchroniser l'horloge du serveur.")

    MAX_HOPS   = int(CFG.get("ROUTER_MAX_HOPS", 3))
    MIN_ATR    = float(CFG.get("MIN_ATR_PCT", 0.3))
    COOLDOWN   = int(CFG.get("COOLDOWN_SEC", 90))
    MAX_ALLOC  = float(CFG.get("MAX_POS_ALLOCATION_PCT", 50.0))

    while True:
        try:
            smap = ex.symbols_map()
            now  = time.time()

            for quote in CFG["QUOTES"]:
                free_q = free_after_reserve(quote, ex.balance('trade', quote))
                logger.info(f"[{quote}] balance libre (après réserve): {free_q}")

                symbols_for_quote = [s for s in CFG["SYMBOLS"] if s.endswith(f"-{quote}")]
                check_positions_for_tp_sl(ex, symbols_for_quote)

                for symbol in symbols_for_quote:
                    # fallback si la paire n'existe pas (ex: SOL-BTC -> SOL-USDT)
                    if symbol not in smap:
                        base, _ = symbol.split('-')
                        fallback = None
                        for q2 in CFG["QUOTES"]:
                            alt = f"{base}-{q2}"
                            if alt in smap:
                                fallback = alt; break
                        if not fallback:
                            logger.warning(f"{symbol} introuvable, aucune alternative trouvée.")
                            continue
                        logger.info(f"{symbol} introuvable → fallback sur {fallback}")
                        symbol = fallback

                    if now < cooldown[symbol]: 
                        continue

                    base, q_cur = symbol.split('-')

                    # init position connue si solde présent au démarrage
                    base_bal = ex.balance('trade', base)
                    if base_bal > 0 and symbol not in positions:
                        last = float(ex.ticker(symbol)['price'])
                        positions[symbol] = {"entry": last, "size": base_bal}
                        logger.info(f"{symbol} position détectée → entry≈{last:.6f}, size={base_bal}")
                        maybe_place_tp_sl(symbol, last)

                    # marché & signal
                    kl = ex.klines(symbol, "15min", limit=120)
                    vol = atr_pct(kl, 14)
                    if vol < MIN_ATR:
                        logger.info(f"{symbol} volatilité faible ({vol:.2f}% ATR), skip.")
                        continue

                    closes = [float(x[2]) for x in kl]
                    sig, reason = ema_cross_signal(closes)
                    logger.info(f"{symbol} signal={sig} ({reason})")

                    # SELL
                    if sig == "sell":
                        base_bal = ex.balance('trade', base)
                        if base_bal > 0:
                            res = ex.place_order(symbol, "sell", size=str(base_bal), type_="market")
                            logger.info(f"{symbol} SELL -> {res}")
                            send_alert(f"SELL {symbol} size={base_bal} (raison: {reason})")
                            positions.pop(symbol, None)
                            cooldown[symbol] = now + COOLDOWN
                        continue

                    # BUY
                    if sig == "buy":
                        if ex.balance('trade', base) > 0 or symbol in positions:
                            logger.info(f"{symbol} déjà en position, skip.")
                            continue

                        alloc = allocation_pct(ex, base, q_cur)
                        if alloc >= MAX_ALLOC:
                            logger.info(f"{symbol} allocation {alloc:.1f}% >= max {MAX_ALLOC}%, skip.")
                            continue

                        # S'il manque de quote pour acheter, on fait du routing multi-hop
                        free_here = free_after_reserve(q_cur, ex.balance('trade', q_cur))
                        if free_here < CFG.get("MIN_TRADE_USDT", 10.0):
                            need = CFG.get("MIN_TRADE_USDT", 10.0) - free_here
                            # Choisir la meilleure quote de départ (celle où on a du solde)
                            start_quotes = sorted(CFG["QUOTES"], key=lambda q: ex.balance('trade', q), reverse=True)
                            path_found = False
                            for q_start in start_quotes:
                                if q_start == q_cur: 
                                    continue
                                bal_start = free_after_reserve(q_start, ex.balance('trade', q_start))
                                if bal_start <= 0: 
                                    continue
                                path = find_quote_path(ex, q_start, q_cur, MAX_HOPS)
                                if path:
                                    logger.info(f"[Router] chemin trouvé {path} pour obtenir {q_cur}")
                                    # On convertit juste le nécessaire (need), limité par bal_start
                                    amt = min(bal_start, need if q_start != "BTC" else bal_start)  # simple borne
                                    execute_quote_path(ex, path, amt)
                                    path_found = True
                                    break
                            # refresh après routage
                            free_here = free_after_reserve(q_cur, ex.balance('trade', q_cur))
                            if not path_found or free_here < CFG.get("MIN_TRADE_USDT", 10.0):
                                logger.info(f"{symbol} pas assez de {q_cur} après routage, skip.")
                                continue

                        quote_amt = calc_order_size_quote(free_here)
                        qty, bid, _ = ensure_qty(ex, symbol, quote_amt)
                        if qty <= 0:
                            logger.info(f"{symbol} qty<=0, skip.")
                            continue

                        res = ex.place_order(symbol, "buy", size=str(qty), type_="market")
                        logger.info(f"{symbol} BUY -> {res}")
                        send_alert(f"BUY {symbol} qty={qty} (raison: {reason})")
                        entry = float(ex.ticker(symbol)['price'])
                        positions[symbol] = {"entry": entry, "size": qty}
                        maybe_place_tp_sl(symbol, entry)
                        cooldown[symbol] = now + COOLDOWN

            time.sleep(CFG.get("POLL_INTERVAL_SEC", 30))

        except KeyboardInterrupt:
            logger.info("Arrêt manuel."); break
        except Exception as e:
            logger.error(f"Loop error: {e}\n{traceback.format_exc()}"); send_alert(f"Erreur loop: {e}"); time.sleep(5)

if __name__ == "__main__":
    run_loop()