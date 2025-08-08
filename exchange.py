import time, math
from tenacity import retry, wait_exponential, stop_after_attempt
from kucoin.client import User, Market, Trade
from config import CFG

class Ku:
    def __init__(self, logger):
        self.logger = logger
        self.user = User(CFG["API_KEY"], CFG["API_SECRET"], CFG["API_PASS"], is_sandbox=CFG["SANDBOX"])
        self.market = Market(is_sandbox=CFG["SANDBOX"])
        self.trade = Trade(CFG["API_KEY"], CFG["API_SECRET"], CFG["API_PASS"], is_sandbox=CFG["SANDBOX"])

    def time_ok(self):
    try:
        # certaines versions n'ont pas get_server_time()
        if hasattr(self.market, 'get_server_time'):
            srv = self.market.get_server_time()
        elif hasattr(self.market, 'get_server_timestamp'):
            srv = self.market.get_server_timestamp()
        else:
            self.logger.info("No server time endpoint; skipping drift check.")
            return 0
        drift = abs(int(srv) - int(time.time()*1000))
        self.logger.info(f"Server time drift ~ {drift} ms")
        return drift
    except Exception as e:
        self.logger.info(f"Time check skipped: {e}")
        return 0

    def accounts(self):
        accs = self.user.get_account_list()
        by_type = {}
        for a in accs:
            by_type.setdefault(a['type'], []).append(a)
        return by_type

    def balance(self, typ, currency):
        by_type = self.accounts()
        return sum(float(a['balance']) for a in by_type.get(typ, []) if a['currency']==currency)

    def symbols_map(self):
        return {s['symbol']: s for s in self.market.get_symbol_list()}

    def ticker(self, symbol):
        return self.market.get_ticker(symbol)

    def klines(self, symbol, ktype="15min", limit=150):
        data = self.market.get_kline(symbol, ktype)
        data = list(reversed(data))[-limit:]
        return data

    def snap_qty(self, qty, step):
        return math.floor(qty / step) * step

    @retry(wait=wait_exponential(multiplier=1, min=1, max=10), stop=stop_after_attempt(5))
    def place_order(self, symbol, side, size=None, price=None, type_="limit"):
        if CFG["DRY_RUN"]:
            self.logger.info(f"[DRY_RUN] place_order {side} {symbol} size={size} price={price} type={type_}")
            return {"orderId": "DRYRUN"}
        if type_ == "market":
            return self.trade.create_market_order(symbol, side, size=size)
        return self.trade.create_limit_order(symbol, side, str(size), str(price))

    def cancel_order(self, order_id):
        if CFG["DRY_RUN"]:
            self.logger.info(f"[DRY_RUN] cancel_order id={order_id}")
            return True
        try:
            self.trade.cancel_order(order_id); return True
        except Exception as e:
            self.logger.warning(f"Cancel failed: {e}"); return False