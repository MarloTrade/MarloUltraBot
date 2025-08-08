def ema(values, period):
    if not values or period <= 0: return []
    k = 2/(period+1); ema_vals = []; ema_prev = values[0]
    for v in values:
        ema_prev = v * k + ema_prev * (1 - k)
        ema_vals.append(ema_prev)
    return ema_vals

def ema_cross_signal(closes):
    if len(closes) < 60: return None, "insuffisant pour EMA"
    ema20 = ema(closes, 20); ema50 = ema(closes, 50)
    e20_prev, e20_last = ema20[-2], ema20[-1]
    e50_prev, e50_last = ema50[-2], ema50[-1]
    if e20_prev <= e50_prev and e20_last > e50_last:
        return "buy", "EMA20 a croisé au-dessus de EMA50"
    if e20_prev >= e50_prev and e20_last < e50_last:
        return "sell", "EMA20 a recroisé sous EMA50"
    return None, "pas de croisement"