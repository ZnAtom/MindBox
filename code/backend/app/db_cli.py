"""Local database administration; no AI calls or automatic candidate promotion."""
import json
from pathlib import Path

from .config import ROOT
from .database import Database
from .scheduling import Scheduling
from .store import Store


def add_parser(sub):
    parser = sub.add_parser('db', help='Manage evidence, inventory transactions and durable reminders')
    parser.add_argument('--database', type=Path, default=ROOT / 'var/mind-box.sqlite3')
    parser.add_argument('--cabinet', default='cabinet-01')
    tasks = parser.add_subparsers(dest='db_command', required=True)
    p = tasks.add_parser('init')
    p.add_argument('--known-empty', action='store_true', help='Explicitly establish an empty initial cabinet')
    tasks.add_parser('status')
    p = tasks.add_parser('import-run')
    p.add_argument('directory', type=Path)
    p.add_argument('--operation-id')
    p.add_argument('--source-key')
    p.add_argument('--base-revision', type=int)
    p = tasks.add_parser('inventory')
    p.add_argument('--include-removed', action='store_true')
    p.add_argument('--query')
    p.add_argument('--location')
    p = tasks.add_parser('item')
    p.add_argument('item_id')
    p = tasks.add_parser('history')
    p.add_argument('item_id')
    p = tasks.add_parser('operation')
    p.add_argument('operation_id')
    p = tasks.add_parser('resolve')
    p.add_argument('run_id')
    p.add_argument('--reason', required=True)
    p.add_argument('--reviewed-by', required=True)
    for name in ('commit', 'create-item', 'correct-item', 'create-reminder'):
        p = tasks.add_parser(name)
        p.add_argument('request', type=Path, help='JSON request file')
    tasks.add_parser('receipts')
    tasks.add_parser('reminders')
    tasks.add_parser('trigger-reminders')
    p = tasks.add_parser('cancel-reminder')
    p.add_argument('reminder_id')
    p = tasks.add_parser('backup')
    p.add_argument('destination', type=Path)


def run(args):
    database = Database(args.database)
    store, scheduling = Store(database), Scheduling(database)
    command = args.db_command
    if command == 'init':
        result = store.initialize(args.cabinet, known_empty=args.known_empty)
    elif command == 'status':
        result = database.status()
    elif command == 'import-run':
        result = store.import_run(args.directory, args.cabinet, args.operation_id,
                                  args.base_revision, args.source_key)
    elif command == 'inventory':
        result = store.get_inventory_snapshot(args.cabinet, args.include_removed, args.query, args.location)
    elif command in ('item', 'history', 'operation'):
        method = {'item': store.get_item, 'history': store.history, 'operation': store.get_operation}[command]
        result = method(args.operation_id if command == 'operation' else args.item_id)
    elif command == 'resolve':
        store.resolve_pending(args.run_id, args.reason, args.reviewed_by)
        result = {'run_id': args.run_id, 'status': 'reviewed', 'inventory_committed': False}
    elif command == 'receipts':
        result = store.receipts(args.cabinet)
    elif command == 'reminders':
        result = scheduling.list_reminders(args.cabinet)
    elif command == 'trigger-reminders':
        result = scheduling.trigger_due()
    elif command == 'cancel-reminder':
        result = scheduling.cancel_reminder(args.reminder_id)
    elif command == 'backup':
        result = database.backup(args.destination)
    else:
        request = json.loads(args.request.read_text(encoding='utf-8'))
        if command == 'commit':
            result = store.commit_inventory(request)
        else:
            method = {'create-item': store.create_item, 'correct-item': store.correct_item,
                      'create-reminder': scheduling.create_reminder}[command]
            result = method(cabinet_id=args.cabinet, **request)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return result
