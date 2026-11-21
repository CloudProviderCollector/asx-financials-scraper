"""Register and run the market Hatchet workflows."""

import logging
import os
import threading
import time
from typing import Any

from hatchet_sdk import Hatchet

from workflows import catch_up
from workflows.asx_200_index_workflow import register_workflow as register_asx_200_index
from workflows.asx_200_profiles_workflow import register_workflows as register_asx_200
from workflows.vea_crude_workflow import register_workflow as register_vea

LOGGER = logging.getLogger(__name__)


def register_workflows() -> tuple[Hatchet, list[Any]]:
    hatchet = Hatchet()
    vea = register_vea(hatchet)
    asx_200 = register_asx_200(hatchet)
    index = register_asx_200_index(hatchet)
    scheduler = catch_up.register_workflow(hatchet, vea, asx_200, index)
    return hatchet, [vea, asx_200, index, scheduler]


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    if not os.getenv("TELEGRAM_BOT_TOKEN") or not os.getenv("TELEGRAM_CHAT_ID"):
        raise RuntimeError("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID before starting worker")
    hatchet, workflows = register_workflows()

    def startup_check() -> None:
        time.sleep(10)
        try:
            catch_up.run_catch_up(hatchet)
        except Exception:
            LOGGER.exception("Market startup catch-up failed; retrying at next cron")

    threading.Thread(target=startup_check, daemon=True, name="market-catch-up").start()
    hatchet.worker("vea-crude-worker", workflows=workflows, slots=2).start()


if __name__ == "__main__":
    main()
