# main.py — MarloTrader ELITE (ensemble + ATR sizing + regime filter)
import time, math, traceback
from collections import defaultdict, deque
from logger_setup import setup_logger
from config import CFG
from exchange import Ku
from telegram_alerts import send_alert

logger = setup_logger()

# ========= State (volatile) =========
positions = {}                 # positions[symbol] = {"entry": float, "size": float}
cooldown  = defaultdict(float) # symbol -> next_allowed_ts

# ========= Indicators =========
def ema(values, period):
    if not values or period <= 0: return []
    k = 2/(period+1)
    out, prev = [], float(values[0])
    for v in values:
        prev = v*k + prev*(1-k)
        out.append(prev)
    return out

def rsi(closes, period=14):
    if len(closes) < period+1: return []
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i]-closes[i-1]
        gains.append(max(diff,0.0)); losses.append(max(-diff,0.0))
    avg_gain = sum(gains[:period])/period
    avg_loss = sum(losses[:period])/period
    rsis = [None]*(period)  # align
    for i in range(period, len(closes)-1):
        gain = gains[i]; loss = losses[i]
        avg_gain = (avg_gain*(period-1)+gain)/period
        avg_loss = (avg_loss*(period-1)+loss)/period
        rs = (avg_gain/avg_loss) if avg_loss>0 else 999999
        rsis.append(100-(100/(1+rs)))
    return rsis

def adx(kl, period=14):
    # kl: [time, open, close, high, low, volume, turnover], oldest->newest
    if len(kl) < period+2: return 0.0
    highs  = [float(k[3]) for k in kl]
    lows   = [float(k[4]) for k in kl]
    closes = [float(k[2]) for k in kl]
    trs, pdms, ndms = [], [], []
    for i in range(1, len(kl)):
        up    = highs[i]-highs[i-1]
        down  = lows[i-1]-lows[i]
        pDM   = up   if (up>down and up>0)   else 0.0
        nDM   = down if (down>up and down>0) else 0.0
        tr = max(highs[i]-lows[i],
                 abs(highs[i]-closes[i-1]),
                 abs(lows[i]-closes[i-1]))
        trs.append(tr); pdms.append(pDM); ndms.append(nDM)
    # Wilder smoothing
    def wilder_smooth(vals, p):
        if len(vals) < p: return []
        sm = [sum(vals[:p])]
        for i in range(p, len(vals)):
            sm.append(sm[-1] - (sm[-1]/p) + vals[i])
        return sm
    trN  = wilder_smooth(trs, period)
    pDMN = wilder_smooth(pdms, period)
    nDMN = wilder_smooth(ndms, period)
    if not (trN and pDMN and nDMN): return 0.0
    di_plus  = [100*(pDMN[i]/trN[i]) if trN[i]>0 else 0 for i in range(len(trN))]
    di_minus = [100*(nDMN[i]/trN[i]) if trN[i]>0 else 0 for i in range(len(trN))]
    dx = [100*abs(di_plus[i]-di_minus[i])/(di_plus[i]+di_minus[i]) if (di_plus[i]+di_minus[i])>0 else 0
          for i in range(len(di_plus))]
    # Average DX (ADX)
    if len(dx) < period: return 0.0
    adx_vals = [sum(dx[:period])/period]
    for i in range(period, len(dx)):
        adx_vals.append((adx_vals[-1]*(period-1)+dx[i])/period)
    return adx_vals[-1]

def atr_pct(kl, period=14):
    if len(kl) < period+1: return 0.0
    trs = []
    for i in range(1, len(kl)):
        h = float(kl[i][3]); l = float(kl[i][4]); pc = float(kl[i-1][2])
        trs.append(max(h-l, abs(h-pc), abs(l-pc)))
    atr = sum(trs[-period:]) / period
    last_close = float(kl[-1][2])
    return (atr/last_close)*100 if last_close>0 else 0.0

# ========= Market utils =========
def spread_pct(t):
    bid = float(t['bestBid']); ask = float(t['bestAsk'])
    mid = (bid+ask)/2 if (bid>0 and ask>0) else 0.0
    return ((ask-bid)/mid*100) if mid>0 else 999.0

def value_in_quote(ex, symbol, base_amount):
    tick = ex.ticker(symbol)
    price = float(tick['price'])
    return base_amount*price

def free_after_reserve(quote, free_quote):
    if quote == "USDT":
        return max(0.0, free_quote - CFG.get("RESERVE_USDT", 20.0))
    if quote == "BTC":
        return max(0.0, free_quote - CFG.get("RESERVE_BTC", 0.0002))
    return free_quote

def calc_position_size_by_atr(ex, symbol, quote: str, kl, risk_usd: float):
    # risque $ / ATR$ ≈ taille en base, puis snap via increments dans ensure_qty
    last = float(kl[-1][2])
    atrp = atr_pct(kl, 14)
    if atrp <= 0: return 0.0
    atr_abs = last*(atrp/100.0)
    if atr_abs <= 0: return 0.0
    # risk in quote (assume quote ~ USD if USDT; for BTC we approximate using pair price)
    # We'll just use risk_usd as "risk in quote"; it's fine for USDT, reasonable for BTC too.
    # Size in base ≈ risk_in_quote / ATR_abs
    base_size = risk_usd / atr_abs
    return max(base_size, 0.0)

# ========= Ensemble signals =========
def signals_ensemble(ex, symbol, kl):
    closes = [float(k[2]) for k in kl]
    highs  = [float(k[3]) for k in kl]
    lows   = [float(k[4]) for k in kl]
    if len(closes) < 60: 
        return None, {"ema":None,"bo":None,"mr":None}

    # 1) EMA Cross
    e20 = ema(closes, 20); e50 = ema(closes, 50)
    ema_sig = None
    if e20[-2] <= e50[-2] and e20[-1] > e50[-1]: ema_sig = "buy"
    elif e20[-2] >= e50[-2] and e20[-1] < e50[-1]: ema_sig = "sell"

    # 2) Breakout (20)
    hh20 = max(highs[-20:]); ll20 = min(lows[-20:])
    bo_sig = None
    if closes[-1] > hh20: bo_sig = "buy"
    elif closes[-1] < ll20: bo_sig = "sell"

    # 3) Mean Revert (RSI)
    r = rsi(closes, 14)
    mr_sig = None
    if r and r[-1] is not None:
        if r[-1] < 30: mr_sig = "buy"
        elif r[-1] > 70: mr_sig = "sell"

    votes = {"ema":ema_sig, "bo":bo_sig, "mr":mr_sig}
    # Résultat final : si >= threshold vote "buy" ou "sell"
    th = int(CFG.get("ENSEMBLE_THRESHOLD", 2))
    b = sum(1 for v in votes.values() if v=="buy")
    s = sum(1 for v in votes.values() if v=="sell")
    final = "buy" if b>=th and b>s else ("sell" if s>=th and s>b else None)
    return final, votes

# ========= Router / pairs =========
def quotes_graph(ex):
    g = defaultdict(set)
    for sym in ex.symbols_map().keys():
        base, quote = sym.split('-')
        g[base].add(quote)   # vendre base -> quote
        g[quote].add(base)   # acheter base en payant quote
    return g

def find_quote_path(ex, start_q, target_q, max_hops=3):
    if start_q == target_q: return [start_q]
    g = quotes_graph(ex)
    seen = {start_q}; dq = deque([(start_q, [start_q])])
    while dq:
        cur, path = dq.popleft()
        if len(path) > max_hops+1: continue
        for nxt in g[cur]:
            if nxt in seen: continue
            np = path+[nxt]
            if nxt == target_q: return np
            seen.add(nxt); dq.append((nxt,np))
    return None

def best_price(ex, pair, side):
    t = ex.ticker(pair)
    return float(t['bestAsk']) if side=="buy" else float(t['bestBid'])

def execute_quote_path(ex, path, amount_in_start):
    if not path or len(path)<2 or amount_in_start<=0: return
    smap = ex.symbols_map()
    amt = amount_in_start
    for i in range(len(path)-1):
        q_from, q_to = path[i], path[i+1]
        sell_pair = f"{q_from}-{q_to}"
        buy_pair  = f"{q_to}-{q_from}"
        if sell_pair in smap:
            size_base = amt
            logger.info(f"[Router] {q_from}->{q_to} via {sell_pair} (sell {size_base} {q_from})")
            ex.place_order(sell_pair, "sell", size=str(size_base), type_="market")
            px = best_price(ex, sell_pair, "sell"); amt = size_base*px
        elif buy_pair in smap:
            px = best_price(ex, buy_pair, "buy")
            size_base = amt/px if px>0 else 0
            if size_base<=0: break
            logger.info(f"[Router] {q_from}->{q_to} via {buy_pair} (buy {size_base} {q_to})")
            ex.place_order(buy_pair, "buy", size=str(size_base), type_="market")
            amt = size_base
        else:
            logger.info(f"[Router] Pas de marché entre {q_from} et {q_to}")
            break

# ========= Core loop =========
def run_loop():
    logger.info(f"Config: {CFG}")
    ex = Ku(logger)
    drift = ex.time_ok()
    if drift > 15000:
        logger.warning("Time drift élevé, pense à resynchroniser l'horloge du serveur.")

    MAX_HOPS  = int(CFG.get("ROUTER_MAX_HOPS", 3))
    MIN_ATR   = float(CFG.get("MIN_ATR_PCT", 0.3))
    COOLDOWN  = int(CFG.get("COOLDOWN_SEC", 90))
    MAX_ALLOC = float(CFG.get("MAX_POS_ALLOCATION_PCT", 50.0))
    MAX_POS   = int(CFG.get("MAX_POSITIONS", 3))
    ADX_MIN   = float(CFG.get("REGIME_ADX_MIN", 18))
    REG_EMA   = int(CFG.get("REGIME_EMA_PERIOD", 200))
    SPREAD_MAX= float(CFG.get("SPREAD_MAX_PCT", 0.25))
    ATR_RISK  = float(CFG.get("ATR_RISK_USD", 15))

    while True:
        try:
            smap = ex.symbols_map()
            now  = time.time()

            # Parcours par quote (USDT, BTC, etc.)
            for quote in CFG["QUOTES"]:
                free_q = free_after_reserve(quote, ex.balance('trade', quote))
                logger.info(f"[{quote}] balance libre (après réserve): {free_q}")

                symbols_for_quote = [s for s in CFG["SYMBOLS"] if s.endswith(f"-{quote}")]

                # Gestion TP/SL côté bot (vend si TP/SL touchés)
                if positions:
                    for sym in list(positions.keys()):
                        if sym.endswith(f"-{quote}") and sym in smap:
                            entry = positions[sym]["entry"]; size = positions[sym]["size"]
                            last  = float(ex.ticker(sym)['price'])
                            if CFG.get("ENABLE_TP_SL", True):
                                tp = entry*(1+CFG.get("TP_PCT",1.5)/100.0)
                                sl = entry*(1-CFG.get("SL_PCT",1.0)/100.0)
                                if last >= tp:
                                    res = ex.place_order(sym, "sell", size=str(size), type_="market")
                                    logger.info(f"{sym} SELL TP -> {res}")
                                    send_alert(f"TP atteint ✅ {sym} ~{last:.6f}")
                                    positions.pop(sym, None)
                                    cooldown[sym] = now + COOLDOWN
                                elif last <= sl:
                                    res = ex.place_order(sym, "sell", size=str(size), type_="market")
                                    logger.info(f"{sym} SELL SL -> {res}")
                                    send_alert(f"SL déclenché ❌ {sym} ~{last:.6f}")
                                    positions.pop(sym, None)
                                    cooldown[sym] = now + COOLDOWN

                # Parcours des symboles de cette quote
                for symbol in symbols_for_quote:
                    # Fallback si la paire est absente
                    if symbol not in smap:
                        base, _ = symbol.split('-')
                        alt = None
                        for q2 in CFG["QUOTES"]:
                            if f"{base}-{q2}" in smap:
                                alt = f"{base}-{q2}"; break
                        if not alt:
                            logger.warning(f"{symbol} introuvable, aucune alternative.")
                            continue
                        logger.info(f"{symbol} introuvable → fallback {alt}")
                        symbol = alt

                    if now < cooldown[symbol]:
                        continue

                    base, q_cur = symbol.split('-')

                    # Découverte position existante au démarrage
                    base_bal = ex.balance('trade', base)
                    if base_bal > 0 and symbol not in positions:
                        last = float(ex.ticker(symbol)['price'])
                        positions[symbol] = {"entry": last, "size": base_bal}
                        logger.info(f"{symbol} position détectée → entry≈{last:.6f}, size={base_bal}")

                    # Regime filter (EMA200 + ADX)
                    kl = ex.klines(symbol, "15min", limit=240)  # plus long pour regime
                    closes = [float(k[2]) for k in kl]
                    if len(closes) < REG_EMA+5:
                        continue
                    e200 = ema(closes, REG_EMA)
                    regime_ok = closes[-1] > e200[-1]
                    cur_adx = adx(kl, 14)
                    if cur_adx < ADX_MIN:
                        regime_ok = False
                    if not regime_ok:
                        logger.info(f"{symbol} regime off (ADX={cur_adx:.1f}, price {'>' if closes[-1]>e200[-1] else '<'} EMA{REG_EMA}).")
                        continue

                    # Spread filter
                    spr = spread_pct(ex.ticker(symbol))
                    if spr > SPREAD_MAX:
                        logger.info(f"{symbol} spread {spr:.2f}% > max {SPREAD_MAX}%, skip.")
                        continue

                    # Volatilité min
                    vol = atr_pct(kl, 14)
                    if vol < MIN_ATR:
                        logger.info(f"{symbol} ATR {vol:.2f}% < {MIN_ATR}%, skip.")
                        continue

                    # Ensemble de signaux (EMA cross + Breakout + MeanRevert)
                    final_sig, votes = signals_ensemble(ex, symbol, kl)
                    logger.info(f"{symbol} ensemble={final_sig} votes={votes}")

                    # SELL (uniquement si position)
                    if final_sig == "sell" and base_bal > 0:
                        res = ex.place_order(symbol, "sell", size=str(base_bal), type_="market")
                        logger.info(f"{symbol} SELL -> {res}")
                        send_alert(f"SELL {symbol} size={base_bal} votes={votes}")
                        positions.pop(symbol, None)
                        cooldown[symbol] = now + COOLDOWN
                        continue

                    # BUY
                    if final_sig == "buy":
                        # Max positions globales
                        if len(positions) >= MAX_POS:
                            logger.info(f"Max positions ({MAX_POS}) atteint, skip buy {symbol}.")
                            continue
                        # Pas de double empilement
                        if base_bal > 0 or symbol in positions:
                            logger.info(f"{symbol} déjà en position, skip.")
                            continue
                        # Allocation max par coin
                        alloc_pct = 0.0
                        if symbol in smap:
                            try: alloc_pct = (value_in_quote(ex, symbol, ex.balance('trade', base)) /
                                              (ex.balance('trade', q_cur)+1e-12))*100.0
                            except: alloc_pct = 0.0
                        if alloc_pct >= MAX_ALLOC:
                            logger.info(f"{symbol} allocation {alloc_pct:.1f}% >= max {MAX_ALLOC}%, skip.")
                            continue

                        # Quote dispo ? sinon router
                        free_here = free_after_reserve(q_cur, ex.balance('trade', q_cur))
                        min_quote = CFG.get("MIN_TRADE_USDT", 10.0)
                        if free_here < min_quote:
                            # multi-hop: trouve chemin depuis la quote avec plus de solde
                            start_quotes = sorted(CFG["QUOTES"], key=lambda q: ex.balance('trade', q), reverse=True)
                            path_found = False
                            for q_start in start_quotes:
                                if q_start == q_cur: continue
                                bal_start = free_after_reserve(q_start, ex.balance('trade', q_start))
                                if bal_start <= 0: continue
                                path = find_quote_path(ex, q_start, q_cur, int(CFG.get("ROUTER_MAX_HOPS", 3)))
                                if path:
                                    need = min_quote - free_here
                                    logger.info(f"[Router] chemin {path} pour obtenir {q_cur} (need≈{need})")
                                    execute_quote_path(ex, path, min(bal_start, need))
                                    path_found = True
                                    break
                            free_here = free_after_reserve(q_cur, ex.balance('trade', q_cur))
                            if not path_found or free_here < min_quote:
                                logger.info(f"{symbol} pas assez de {q_cur} après routage, skip.")
                                continue

                        # Position sizing par ATR
                        base_target = calc_position_size_by_atr(ex, symbol, q_cur, kl, float(CFG.get("ATR_RISK_USD", 15)))
                        if base_target <= 0:
                            logger.info(f"{symbol} sizing ATR nul, skip.")
                            continue

                        # Convertir en quote montant et snap qty
                        bid = float(ex.ticker(symbol)['bestBid'])
                        quote_amt = base_target * bid
                        # hard cap par free_here
                        quote_amt = min(quote_amt, free_here)
                        if quote_amt < min_quote:
                            logger.info(f"{symbol} quote_amt {quote_amt:.4f} < min {min_quote}, skip.")
                            continue

                        # Ensure qty via increments (price/size/minFunds)
                        smap_local = ex.symbols_map()[symbol]
                        size_step = float(smap_local['baseIncrement'])
                        qty = math.floor((quote_amt / bid)/size_step)*size_step
                        if qty <= 0:
                            logger.info(f"{symbol} qty<=0 après snap, skip.")
                            continue

                        res = ex.place_order(symbol, "buy", size=str(qty), type_="market")
                        logger.info(f"{symbol} BUY -> {res}")
                        send_alert(f"BUY {symbol} qty={qty} votes={votes}")
                        entry = float(ex.ticker(symbol)['price'])
                        positions[symbol] = {"entry": entry, "size": qty}
                        # TP/SL info
                        if CFG.get("ENABLE_TP_SL", True):
                            tp = entry*(1+CFG.get("TP_PCT",1.5)/100.0)
                            sl = entry*(1-CFG.get("SL_PCT",1.0)/100.0)
                            logger.info(f"{symbol} TP/SL armés → TP≈{tp:.6f} / SL≈{sl:.6f}")
                            send_alert(f"{symbol} TP/SL armés → TP≈{tp:.6f} / SL≈{sl:.6f}")
                        cooldown[symbol] = now + COOLDOWN

            time.sleep(CFG.get("POLL_INTERVAL_SEC", 30))

        except KeyboardInterrupt:
            logger.info("Arrêt manuel."); break
        except Exception as e:
            logger.error(f"Loop error: {e}\n{traceback.format_exc()}"); send_alert(f"Erreur loop: {e}"); time.sleep(5)

if __name__ == "__main__":
    run_loop()