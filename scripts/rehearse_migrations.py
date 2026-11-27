"""Rehearse every pending schema migration on a COPY of a database.

    py scripts/rehearse_migrations.py                     # copies $DB_NAME into $DB_NAME_rehearsal
    py scripts/rehearse_migrations.py --source pgpc_db --rollback --keep

What it does (the source database is only READ):
  1. copies the source into <source>_rehearsal (dropped first; the name must end in _rehearsal),
  2. records row counts and id sums of every table,
  3. applies all pending migrations, timing each one,
  4. checks that no pre-existing table lost a row or changed its ids, and that nothing is pending,
  5. runs scripts/audit_data.py against the copy,
  6. with --rollback: rolls every applied migration back in reverse order (only where its
     rollback guard allows) and applies them again, checking the counts once more,
  7. drops the copy unless --keep.
Exit code 0 = rehearsal passed.
"""

import argparse
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from dotenv import load_dotenv

load_dotenv(os.path.join(ROOT, ".env"))

import mysql.connector

from website.schema_migrations import MIGRATIONS, apply_all, pending, applied_ids


def connect(database=None):
    return mysql.connector.connect(host=os.getenv("DB_HOST", "localhost"), port=int(os.getenv("DB_PORT", "3306")),
                                   user=os.getenv("DB_USER", "root"), password=os.getenv("DB_PASSWORD", ""),
                                   database=database, autocommit=True)


def tables(cur, db):
    cur.execute("SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA = %s AND TABLE_TYPE = 'BASE TABLE' "
                "ORDER BY TABLE_NAME", (db,))
    return [r[0] for r in cur.fetchall()]


def fingerprint(cur, db):
    """{table: (rows, sum of the first primary-key column)}"""
    out = {}
    for t in tables(cur, db):
        cur.execute("SELECT COLUMN_NAME FROM information_schema.KEY_COLUMN_USAGE WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s "
                    "AND CONSTRAINT_NAME = 'PRIMARY' ORDER BY ORDINAL_POSITION LIMIT 1", (db, t))
        pk = cur.fetchone()
        if pk:
            cur.execute(f"SELECT COUNT(*), COALESCE(SUM(CRC32(`{pk[0]}`)), 0) FROM `{db}`.`{t}`")
        else:
            cur.execute(f"SELECT COUNT(*), 0 FROM `{db}`.`{t}`")
        out[t] = tuple(int(x) for x in cur.fetchone())
    return out


def clone(cur, source, target):
    cur.execute(f"DROP DATABASE IF EXISTS `{target}`")
    cur.execute(f"CREATE DATABASE `{target}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
    cur.execute("SET FOREIGN_KEY_CHECKS = 0")
    for t in tables(cur, source):
        cur.execute(f"CREATE TABLE `{target}`.`{t}` LIKE `{source}`.`{t}`")
        cur.execute(f"INSERT INTO `{target}`.`{t}` SELECT * FROM `{source}`.`{t}`")
    # CREATE TABLE ... LIKE does not copy foreign keys: copy them so the rehearsal sees them
    cur.execute("""SELECT rc.TABLE_NAME, rc.CONSTRAINT_NAME, GROUP_CONCAT(k.COLUMN_NAME ORDER BY k.ORDINAL_POSITION),
                          rc.REFERENCED_TABLE_NAME, GROUP_CONCAT(k.REFERENCED_COLUMN_NAME ORDER BY k.ORDINAL_POSITION),
                          rc.DELETE_RULE, rc.UPDATE_RULE
                   FROM information_schema.REFERENTIAL_CONSTRAINTS rc
                   JOIN information_schema.KEY_COLUMN_USAGE k ON k.CONSTRAINT_SCHEMA = rc.CONSTRAINT_SCHEMA
                        AND k.CONSTRAINT_NAME = rc.CONSTRAINT_NAME AND k.TABLE_NAME = rc.TABLE_NAME
                   WHERE rc.CONSTRAINT_SCHEMA = %s
                   GROUP BY rc.TABLE_NAME, rc.CONSTRAINT_NAME, rc.REFERENCED_TABLE_NAME, rc.DELETE_RULE, rc.UPDATE_RULE""", (source,))
    for t, name, cols, ref, refcols, on_del, on_upd in cur.fetchall():
        cols = ', '.join(f'`{c}`' for c in cols.split(','))
        refcols = ', '.join(f'`{c}`' for c in refcols.split(','))
        cur.execute(f"ALTER TABLE `{target}`.`{t}` ADD CONSTRAINT `{name}` FOREIGN KEY ({cols}) "
                    f"REFERENCES `{target}`.`{ref}` ({refcols}) ON DELETE {on_del} ON UPDATE {on_upd}")
    cur.execute("SET FOREIGN_KEY_CHECKS = 1")


def apply_timed(conn):
    timings, last = [], [time.perf_counter()]

    def log(line):
        if line.startswith('[OK] '):
            now = time.perf_counter()
            timings.append((line[5:].strip(), now - last[0]))
            last[0] = now
    apply_all(conn, log=log)
    return timings


def compare(before, after, label):
    problems = []
    for t, (rows, ids) in before.items():
        if t not in after:
            problems.append(f'{label}: table {t} disappeared')
        elif after[t] != (rows, ids) and t != 'schema_migrations':
            problems.append(f'{label}: {t} changed from {rows} rows to {after[t][0]} rows (or its ids changed)')
    return problems


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--source', default=os.getenv('DB_NAME'))
    p.add_argument('--rollback', action='store_true', help='also roll back and re-apply')
    p.add_argument('--keep', action='store_true', help='keep the copy afterwards')
    args = p.parse_args()
    source, target = args.source, f'{args.source}_rehearsal'
    if not source or source.endswith('_rehearsal'):
        print('Give the source database (not a rehearsal copy).')
        return 2
    server = connect()
    cur = server.cursor()
    print(f'# Migration rehearsal: {source} -> {target}\n')

    t0 = time.perf_counter()
    clone(cur, source, target)
    print(f'Copied {len(tables(cur, target))} tables in {time.perf_counter() - t0:.1f}s')
    before = fingerprint(cur, target)
    print(f'Rows before: {sum(r for r, _ in before.values()):,}')

    conn = connect(target)
    todo = [m['id'] for m in pending(conn)]
    print(f'Pending: {", ".join(todo) or "none"}\n')
    problems = []
    timings = apply_timed(conn)
    for mid, secs in timings:
        print(f'  applied {mid:<40} {secs:8.2f}s')
    after = fingerprint(cur, target)
    problems += compare(before, after, 'apply')
    if pending(conn):
        problems.append('still pending after apply: ' + ', '.join(m['id'] for m in pending(conn)))
    print(f'\nNew tables: {", ".join(sorted(set(after) - set(before))) or "none"}')

    env = dict(os.environ, DB_NAME=target)
    audit = subprocess.run([sys.executable, os.path.join(ROOT, 'scripts', 'audit_data.py')], env=env,
                           capture_output=True, text=True)
    high = [l for l in audit.stdout.splitlines() if ' high ' in l and 'OK' not in l]
    print(f'Data audit: exit {audit.returncode}, {len(high)} high-severity finding(s)')
    for line in high:
        print('   ', line)

    if args.rollback:
        done = applied_ids(conn)
        rolled = []
        rc = conn.cursor()
        for m in reversed(MIGRATIONS):
            if m['id'] not in done or m['id'] not in todo:
                continue
            if m.get('rollback_guard'):
                rc.execute(m['rollback_guard'])
                if (rc.fetchone() or [0])[0]:
                    print(f'  rollback {m["id"]}: refused by its guard (data present), stopping here')
                    break
            for sql in m['rollback']:
                rc.execute(sql)
            rc.execute("DELETE FROM schema_migrations WHERE id = %s", (m['id'],))
            rolled.append(m['id'])
        print(f'\nRolled back: {", ".join(rolled) or "none"}')
        problems += compare(before, fingerprint(cur, target), 'rollback')
        timings2 = apply_timed(conn)
        print(f'Re-applied: {", ".join(m for m, _ in timings2) or "none"}')
        problems += compare(before, fingerprint(cur, target), 're-apply')

    conn.close()
    if not args.keep:
        cur.execute(f"DROP DATABASE `{target}`")
    print('\nRESULT: ' + ('PASSED' if not problems else 'FAILED'))
    for pr in problems:
        print('  - ' + pr)
    return 0 if not problems else 1


if __name__ == '__main__':
    sys.exit(main())
