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

    # Quotes (ex: USDT,BTC). Si QUOTES absent, on retombe sur QUOTE unique.
    "QUOTE": os.getenv("QUOTE", "USDT"),
    "QUOTES": [s.strip() for s in os.getenv("QUOTES", os.getenv("QUOTE", "USDT")).split(",") if s.strip()],

    "SYMBOLS": [s.strip() for s in os.getenv("SYMBOLS", "BTC-USDT,ETH-USDT").split(",") if s.strip()],
    "MIN_TRADE_USDT": float(os.getenv("MIN_TRADE_USDT", "10")),
    "RISK_PCT": float(os.getenv("RISK_PCT", "10")),
    "STRATEGY": os.getenv("STRATEGY", "EMA_CROSS"),
    "POLL_INTERVAL_SEC": int(os.getenv("POLL_INTERVAL_SEC", "30")),

    "ENABLE_TP_SL": os.getenv("ENABLE_TP_SL", "true").lower() == "true",
    "TP_PCT": float(os.getenv("TP_PCT", "1.5")),   # +1.5% par défaut
    "SL_PCT": float(os.getenv("SL_PCT", "1.0")),   # -1.0% par défaut

    "TELEGRAM_TOKEN": os.getenv("TELEGRAM_TOKEN"),
    "TELEGRAM_CHAT_ID": os.getenv("TELEGRAM_CHAT_ID"),
}

# Erreur claire si clés manquantes
for k in ["API_KEY", "API_SECRET", "API_PASS"]:
    if not CFG[k]:
        raise RuntimeError(f"Missing env: {k}. Set it on Render → Environment.")