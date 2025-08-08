import logging, os

def setup_logger():
    os.makedirs("logs", exist_ok=True)
    logger = logging.getLogger("marlo")
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")
    ch = logging.StreamHandler(); ch.setFormatter(fmt)
    fh = logging.FileHandler("logs/app.log"); fh.setFormatter(fmt)
    if not logger.handlers:
        logger.addHandler(ch); logger.addHandler(fh)
    return logger