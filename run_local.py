"""Continuous local runner for Cita Zarwal.

Runs appointment checks at regular intervals directly from your local machine,
avoiding foreign cloud datacenter geoblocking (e.g. from GitHub Actions).
"""

import logging
import os
import sys
import time

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import scraper

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
)
LOG = logging.getLogger("cita-local-runner")

CHECK_INTERVAL_MINUTES = int(os.getenv("CHECK_INTERVAL_MINUTES", "15"))


def run_loop():
    LOG.info("Starting local Cita Zarwal loop (interval: %d minutes)", CHECK_INTERVAL_MINUTES)
    print(f"🚀 Cita Zarwal Local Tracker started.")
    print(f"Checking every {CHECK_INTERVAL_MINUTES} minutes. Press Ctrl+C to stop.\n")

    iteration = 1
    while True:
        try:
            print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Running check #{iteration}...")
            exit_code = scraper.main()
            LOG.info("Check #%d completed with status code %d", iteration, exit_code)
        except KeyboardInterrupt:
            print("\nStopped by user.")
            break
        except Exception as exc:
            LOG.exception("Unexpected error during check #%d: %s", iteration, exc)

        iteration += 1
        print(f"Waiting {CHECK_INTERVAL_MINUTES} minutes until next check...\n")
        time.sleep(CHECK_INTERVAL_MINUTES * 60)


if __name__ == "__main__":
    run_loop()
