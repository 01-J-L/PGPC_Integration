"""Apply PGPC schema migrations (see website/schema_migrations.py).

    python scripts/migrate.py status
    python scripts/migrate.py apply --backup-done
    python scripts/migrate.py rollback <migration id> --backup-done [--force]

`apply` and `rollback` refuse to run unless you confirm with --backup-done
that a fresh database backup exists (see docs/PHASE1_AUDIT_REPORT.md,
"Backup and rollback").
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(ROOT, '.env'))

import mysql.connector  # noqa: E402

from website.schema_migrations import MIGRATIONS, applied_ids, pending, apply_all  # noqa: E402


def connect():
    return mysql.connector.connect(
        host=os.getenv('DB_HOST', 'localhost'), port=int(os.getenv('DB_PORT', '3306')),
        user=os.getenv('DB_USER', 'root'), password=os.getenv('DB_PASSWORD', ''),
        database=os.getenv('DB_NAME'))


def main(argv):
    if not argv or argv[0] not in ('status', 'apply', 'rollback'):
        print(__doc__)
        return 2
    conn = connect()
    print(f"Database: {os.getenv('DB_NAME')} @ {os.getenv('DB_HOST', 'localhost')}")
    cmd = argv[0]
    if cmd == 'status':
        done = applied_ids(conn)
        for m in MIGRATIONS:
            print(f"  [{'applied' if m['id'] in done else 'PENDING'}] {m['id']}: {m['description']}")
        return 0
    if '--backup-done' not in argv:
        print("Refusing to change the schema: take a backup first, then re-run with --backup-done.")
        return 3
    if cmd == 'apply':
        todo = pending(conn)
        if not todo:
            print("Nothing to apply.")
            return 0
        apply_all(conn)
        return 0
    if cmd == 'rollback':
        target = argv[1] if len(argv) > 1 and not argv[1].startswith('--') else None
        mig = next((m for m in MIGRATIONS if m['id'] == target), None)
        if not mig:
            print("Unknown migration id.")
            return 2
        cur = conn.cursor()
        if mig.get('rollback_guard') and '--force' not in argv:
            cur.execute(mig['rollback_guard'])
            if (cur.fetchone() or [0])[0]:
                print("Refusing: these tables contain data that a rollback would DELETE. "
                      "Export them first, then re-run with --force.")
                return 4
        for sql in mig['rollback']:
            print(f"  [run ] {sql}")
            cur.execute(sql)
        cur.execute("DELETE FROM schema_migrations WHERE id = %s", (mig['id'],))
        conn.commit()
        print(f"[OK] rolled back {mig['id']}")
        return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
