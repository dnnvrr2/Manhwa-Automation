import logging
from pathlib import Path

def configure_logging() -> None:
    log = Path.home() / "AppData" / "Local" / "ManhwaAutomation" / "manhwa_automation.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=log, level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
