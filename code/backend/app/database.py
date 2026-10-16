"""SQLite connections, checksum-verified migrations and consistent local backups."""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
from uuid import uuid4

MIGRATIONS = Path(__file__).resolve().parents[1] / 'migrations'


class DatabaseError(ValueError):
    pass


class Conflict(DatabaseError):
    pass


class NotFound(DatabaseError):
    pass


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec='microseconds')


def timestamp(value):
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise DatabaseError('A timezone-aware timestamp is required')
    return parsed.astimezone(timezone.utc).isoformat(timespec='microseconds')


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def fingerprint(value):
    return hashlib.sha256(encode(value).encode('utf-8')).hexdigest()


def new_id():
    return str(uuid4())


class Database:
    def __init__(self, path):
        self.path = Path(path).resolve()
        self.assets = self.path.parent / (self.path.stem + '-assets')

    @contextmanager
    def connect(self, create=False):
        if not create and not self.path.is_file():
            raise NotFound('Initialize the database before accessing it: ' + str(self.path))
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA foreign_keys = ON')
        connection.execute('PRAGMA busy_timeout = 10000')
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def transaction(self):
        with self.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            try:
                yield connection
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def migrate(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect(create=True) as connection:
            connection.execute('PRAGMA journal_mode = WAL')
        with self.transaction() as connection:
            connection.execute('CREATE TABLE IF NOT EXISTS schema_migrations '
                               '(version TEXT PRIMARY KEY, sha256 TEXT NOT NULL, applied_at TEXT NOT NULL)')
            available = {path.stem: path for path in MIGRATIONS.glob('*.sql')}
            applied = {row['version']: row['sha256'] for row in connection.execute('SELECT * FROM schema_migrations')}
            if set(applied) - set(available):
                raise DatabaseError('Database has migrations newer than this application')
            for version, path in sorted(available.items()):
                content = path.read_text(encoding='utf-8')
                digest = hashlib.sha256(content.encode('utf-8')).hexdigest()
                if version in applied:
                    if applied[version] != digest:
                        raise DatabaseError('Applied migration checksum changed: ' + version)
                    continue
                # executescript commits implicitly; execute complete SQL statements instead.
                statement = ''
                for line in content.splitlines(keepends=True):
                    statement += line
                    if sqlite3.complete_statement(statement):
                        connection.execute(statement)
                        statement = ''
                if statement.strip():
                    raise DatabaseError('Incomplete migration: ' + version)
                connection.execute('INSERT INTO schema_migrations VALUES (?, ?, ?)', (version, digest, utcnow()))
        return self.status()

    def status(self):
        with self.connect() as connection:
            return {'path': str(self.path), 'journal_mode': connection.execute('PRAGMA journal_mode').fetchone()[0],
                    'integrity': connection.execute('PRAGMA integrity_check').fetchone()[0],
                    'foreign_key_errors': [tuple(r) for r in connection.execute('PRAGMA foreign_key_check')],
                    'migrations': [dict(r) for r in connection.execute('SELECT * FROM schema_migrations ORDER BY version')],
                    'tables': [r[0] for r in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]}

    def backup(self, destination):
        destination = Path(destination).resolve()
        if destination == self.path.parent or destination in self.assets.parents or self.assets in destination.parents:
            raise DatabaseError('Backup must use a separate directory outside the asset tree')
        destination.mkdir(parents=True, exist_ok=False)
        backup_path = destination / self.path.name
        # Hold the writer lock so the asset set corresponds to the database snapshot.
        with self.transaction():
            if self.assets.exists():
                shutil.copytree(self.assets, destination / self.assets.name)
            with self.connect() as source, sqlite3.connect(backup_path) as target:
                source.backup(target)
        return {'database': str(backup_path), 'assets': str(destination / self.assets.name),
                'note': 'Archived images included; source videos remain external references.'}
