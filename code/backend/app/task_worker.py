"""Restartable periodic scan; business deadlines and delivery jobs live in SQLite."""
import argparse
import json
from pathlib import Path
import time

from apscheduler.schedulers.background import BackgroundScheduler

from .client import write_json
from .experiments import Experiment
from .tasks import Tasks


def run(context, seconds=5, output=None):
    tasks = Tasks(context)
    ticks = []

    def scan():
        ticks.append({'invalidated': tasks.invalidate_changed_dates(), 'rules': tasks.scan_rules(),
                      'due': tasks.scheduling.trigger_due()})

    scheduler = BackgroundScheduler(timezone='UTC')
    scheduler.add_job(scan, 'interval', seconds=0.5, max_instances=1, coalesce=True)
    scheduler.start()
    try:
        time.sleep(max(0, min(60, seconds)))
    finally:
        scheduler.shutdown(wait=True)
    result = {'ticks': ticks, 'reminders': tasks.scheduling.list_reminders('cabinet-01'),
              'delivery': 'queued_only_no_device_or_browser_ack', 'simulation': True}
    if output:
        write_json(output, result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('experiment', type=Path)
    parser.add_argument('--seconds', type=int, default=5)
    args = parser.parse_args()
    context = Experiment.open(args.experiment)
    print(json.dumps(run(context, args.seconds, context.root / 'scheduler-report.json'), ensure_ascii=False))


if __name__ == '__main__':
    main()
