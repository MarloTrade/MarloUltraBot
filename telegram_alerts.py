import requests
from config import CFG

def send_alert(text: str):
    token = CFG["TELEGRAM_TOKEN"]; chat_id = CFG["TELEGRAM_CHAT_ID"]
    if not token or not chat_id: return
    try:
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      json={"chat_id": chat_id, "text": text})
    except Exception:
        pass