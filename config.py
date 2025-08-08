import os
from dotenv import load_dotenv
load_dotenv()

CFG = {
    "API_KEY": os.getenv("KUCOIN_API_KEY"),
    "API_SECRET": os.getenv("KUCOIN_API_SECRET"),
    "API_PASS": os.getenv("KUCOIN_API_PASSPHRASE"),
    "SANDBOX": os.getenv("KUCOIN_SANDBOX", "false").lower() == "true",
    "DRY_RUN": os.getenv("DRY_RUN", "true").lower() == "true",
    "TEST_TRADE": os.getenv("TEST_TRADE", "false").lower() == "true",
    "QUOTE": os.getenv("QUOTE", "USDT"),
    "SYMBOLS": [s.strip() for s in os.getenv("SYMBOLS", "BTC-USDT").split(",") if s.strip()],
    "MIN_TRADE_USDT": float(os.getenv("MIN_TRADE_USDT", "10")),
    "RISK_PCT": float(os.getenv("RISK_PCT", "10")),
    "STRATEGY": os.getenv("STRATEGY", "EMA_CROSS"),
    "POLL_INTERVAL_SEC": int(os.getenv("POLL_INTERVAL_SEC", "30")),
    "ENABLE_TP_SL": os.getenv("ENABLE_TP_SL", "true").lower() == "true",
    "TP_PCT": float(os.getenv("TP_PCT", "1.5")),
    "SL_PCT": float(os.getenv("SL_PCT", "1.0")),
    "TELEGRAM_TOKEN": os.getenv("TELEGRAM_TOKEN"),
    "TELEGRAM_CHAT_ID": os.getenv("TELEGRAM_CHAT_ID"),
}