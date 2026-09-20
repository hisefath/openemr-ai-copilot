"""Entry point for the Railway `alerts` cron service, whose start command (`python alerts.py`) is configured on the
service, outside this repository, and so cannot move with the code. The evaluator itself is copilot/alerts.py; run it
directly as `python -m copilot.alerts [<from> <to>]`. Delete this file once the service's start command is updated.
"""
import sys

from copilot.alerts import main

if __name__ == "__main__":
    sys.exit(main())
