"""Owner-only local administration. Complete keys are printed only when created."""
import argparse
import json

from .api_keys import KeyStore
from .config import ROOT, Settings
from .database import Database


def main():
    import os
    Settings.load()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', default=os.getenv('MIND_BOX_DATABASE', ROOT / 'var/mind-box.sqlite3'))
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('pair', help='Create a one-use local owner pairing code, valid for ten minutes')
    sub.add_parser('list')
    create = sub.add_parser('create')
    create.add_argument('--name', required=True)
    create.add_argument('--cabinet', action='append', required=True)
    create.add_argument('--scope', action='append', default=['inventory:read'])
    create.add_argument('--days', type=int, default=90)
    for command in ('revoke', 'rotate'):
        sub.add_parser(command).add_argument('id')
    args = parser.parse_args()
    database = Database(args.database)
    if not database.path.is_file():
        parser.error('Initialize the inventory database first')
    database.migrate()
    keys = KeyStore(database)
    if args.command == 'pair':
        print(keys.pairing_code())
    elif args.command == 'list':
        print(json.dumps(keys.list(), ensure_ascii=False, indent=2))
    elif args.command == 'create':
        print(json.dumps(keys.create(args.name, args.cabinet, args.scope, args.days), ensure_ascii=False, indent=2))
    elif args.command == 'revoke':
        keys.revoke(args.id)
        print('Connection revoked')
    else:
        print(json.dumps(keys.rotate(args.id), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
