"""Phase 3 - unify the two identity tables into one `users` table.

Before this migration the app has two account stores:

    users         -> deans + teachers (faculty portal, Flask-Login)
    system_users  -> admission/grading staff (raw session auth)

Both use integer primary keys, so Flask-Login's bare integer session id
resolves against `users` first and silently returns the wrong person when
the id also exists in `system_users` (users.id=1 adminccba vs
system_users.id=1 admin).  This script makes `users` the single identity
table while keeping the two login portals separate.

What it does
------------
1. Snapshots `system_users` into `system_users_backup_<date>` (rollback copy).
2. Extends `users` with: system, can_edit, created_at, password_hash_alt.
3. Merges rows that describe the same person (matched on email).
4. Inserts the remaining system_users rows into `users`.
5. Repoints the two foreign keys that referenced `system_users`
   (faculty_assignments.faculty_id, grades.faculty_id) to `users.id`.
6. Leaves the old `system_users` table untouched, read-only, for rollback.

Usage
-----
    py scripts/phase3_unify_users.py                     # dry run, prints plan
    py scripts/phase3_unify_users.py --apply             # performs migration
    py scripts/phase3_unify_users.py --apply --database other_db

The script is idempotent: if `users.system` already exists it exits without
changing anything.
"""

import argparse
import os
import sys
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from dotenv import load_dotenv

load_dotenv(os.path.join(ROOT, ".env"))

import mysql.connector

USER_COLUMNS = {
    "system": "ADD COLUMN `system` VARCHAR(20) NOT NULL DEFAULT 'faculty'",
    "can_edit": "ADD COLUMN `can_edit` TINYINT(1) NOT NULL DEFAULT 1",
    "created_at": "ADD COLUMN `created_at` DATETIME NULL DEFAULT CURRENT_TIMESTAMP",
    "password_hash_alt": "ADD COLUMN `password_hash_alt` VARCHAR(256) NULL",
}

FK_TABLES = (
    ("faculty_assignments", "faculty_assignments_ibfk_1"),
    ("grades", "grades_ibfk_3"),
)


def connect(database=None):
    return mysql.connector.connect(
        host=os.getenv("DB_HOST", "localhost"),
        port=int(os.getenv("DB_PORT", "3306")),
        user=os.getenv("DB_USER", "root"),
        password=os.getenv("DB_PASSWORD", ""),
        database=database or os.getenv("DB_NAME", "expired"),
        autocommit=False,
    )


def column_exists(cur, table, column):
    cur.execute(
        "SELECT COUNT(*) AS n FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s AND COLUMN_NAME = %s",
        (table, column),
    )
    return cur.fetchone()["n"] > 0


def constraint_exists(cur, table, constraint):
    cur.execute(
        "SELECT COUNT(*) AS n FROM information_schema.TABLE_CONSTRAINTS "
        "WHERE CONSTRAINT_SCHEMA = DATABASE() AND TABLE_NAME = %s AND CONSTRAINT_NAME = %s",
        (table, constraint),
    )
    return cur.fetchone()["n"] > 0


def already_applied(cur):
    """True only when the rows were moved *and* the foreign keys were repointed,
    so a run that failed halfway is resumable."""
    if not column_exists(cur, "users", "system"):
        return False
    cur.execute("SELECT COUNT(*) AS n FROM users WHERE `system` IN ('admission', 'grading')")
    if cur.fetchone()["n"] == 0:
        return False
    for table, constraint in FK_TABLES:
        if not references_users(cur, table, constraint):
            return False
    return True


def references_users(cur, table, constraint):
    cur.execute(
        "SELECT REFERENCED_TABLE_NAME AS ref FROM information_schema.KEY_COLUMN_USAGE "
        "WHERE CONSTRAINT_SCHEMA = DATABASE() AND TABLE_NAME = %s AND CONSTRAINT_NAME = %s",
        (table, constraint),
    )
    row = cur.fetchone()
    return bool(row and row["ref"] == "users")


def fetch_plan(cur):
    """Return (merged, inserted) row plans."""
    cur.execute("SELECT * FROM users")
    users = {r["id"]: r for r in cur.fetchall()}

    cur.execute("SELECT * FROM system_users")
    sysusers = cur.fetchall()

    by_email = {}
    for uid, row in users.items():
        email = (row.get("email") or "").strip().lower()
        if email:
            by_email[email] = uid

    merged, inserted = [], []
    taken_ids = set(users)

    for su in sysusers:
        email = (su.get("email") or "").strip().lower()
        target = by_email.get(email)
        if target:
            merged.append((su, target, users[target]))
            continue

        preferred = su["id"] if su["id"] not in taken_ids else None
        if preferred is not None:
            taken_ids.add(preferred)
        inserted.append((su, preferred))

    return users, merged, inserted


def describe(users, merged, inserted):
    print("  users rows:", len(users))
    print("  system_users rows:", len(merged) + len(inserted))
    print()
    if merged:
        print("  MERGE (same person, matched on email)")
        for su, target, existing in merged:
            print(f"    system_users.id={su['id']} ({su['username']}) "
                  f"-> users.id={target} ({existing.get('username') or 'NULL'})")
            if existing.get("password_hash") and existing["password_hash"] != su["password"]:
                print("      both rows have a password hash -> keep the users hash as primary,")
                print("      store the system_users hash in password_hash_alt so both passwords still work")
            if not existing.get("username"):
                print(f"      adopt username '{su['username']}'")
            if not existing.get("department") and su.get("department"):
                print(f"      adopt department '{su['department']}'")
    print()
    print("  INSERT (new users row)")
    for su, newid in inserted:
        note = f"keep id={newid}" if newid else f"new auto id (system_users id={su['id']} is taken in users)"
        print(f"    system_users.id={su['id']} ({su['username']}, {su['role']}, system={su.get('system')}) -> {note}")


def apply_migration(conn, dry_run):
    cur = conn.cursor(dictionary=True)

    if already_applied(cur):
        print("Already applied: `users.system` exists. Nothing to do.")
        return 0

    users, merged, inserted = fetch_plan(cur)
    describe(users, merged, inserted)

    if dry_run:
        print("\nDry run only. Re-run with --apply to perform the migration.")
        return 0

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = f"system_users_backup_{stamp}"

    cur.execute(f"CREATE TABLE `{backup}` AS SELECT * FROM `system_users`")
    print(f"\n[1/6] snapshot created: {backup}")

    for column, ddl in USER_COLUMNS.items():
        if not column_exists(cur, "users", column):
            cur.execute(f"ALTER TABLE `users` {ddl}")
    cur.execute(
        "SELECT IS_NULLABLE FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'users' AND COLUMN_NAME = 'email'"
    )
    if cur.fetchone()["IS_NULLABLE"] == "NO":
        cur.execute("ALTER TABLE `users` MODIFY COLUMN `email` VARCHAR(120) NULL")
    print("[2/6] users extended (system, can_edit, created_at, password_hash_alt; email nullable)")

    for su, target, existing in merged:
        fields = ["`system` = 'faculty'", "status = 'active'"]
        params = []
        if not existing.get("username"):
            fields.append("username = %s")
            params.append(su["username"])
        if not existing.get("department") and su.get("department"):
            fields.append("department = %s")
            params.append(su["department"])
        if existing.get("password_hash") and existing["password_hash"] != su["password"]:
            fields.append("password_hash_alt = %s")
            params.append(su["password"])
        elif not existing.get("password_hash"):
            fields.append("password_hash = %s")
            params.append(su["password"])
        params.append(target)
        cur.execute(f"UPDATE `users` SET {', '.join(fields)} WHERE id = %s", params)
    print(f"[3/6] merged {len(merged)} duplicate account(s)")

    id_map = {}
    for su, newid in inserted:
        cols = ["username", "password_hash", "role", "full_name", "created_at",
                "can_edit", "department", "email", "`system`", "status"]
        vals = [su["username"], su["password"], su["role"], su["full_name"],
                su.get("created_at"), 1 if su.get("can_edit") is None else int(su["can_edit"]),
                su.get("department"), su.get("email"), su.get("system") or "admission", "active"]
        if newid is not None:
            cols.append("id")
            vals.append(newid)
        placeholders = ", ".join(["%s"] * len(cols))
        cur.execute(f"INSERT IGNORE INTO `users` ({', '.join(cols)}) VALUES ({placeholders})", vals)
        id_map[su["id"]] = newid if newid is not None else cur.lastrowid
    for su, target, _ in merged:
        id_map[su["id"]] = target
    print(f"[4/6] inserted {len(inserted)} account(s); id map: {id_map}")

    for table, constraint in FK_TABLES:
        if references_users(cur, table, constraint):
            continue
        if constraint_exists(cur, table, constraint):
            cur.execute(f"ALTER TABLE `{table}` DROP FOREIGN KEY `{constraint}`")
        for old_id, new_id in id_map.items():
            if old_id != new_id:
                cur.execute(f"UPDATE `{table}` SET faculty_id = %s WHERE faculty_id = %s", (new_id, old_id))
        cur.execute(
            f"ALTER TABLE `{table}` ADD CONSTRAINT `{constraint}` "
            f"FOREIGN KEY (faculty_id) REFERENCES `users` (id)"
        )
    print("[5/6] faculty_assignments.faculty_id and grades.faculty_id now reference users.id")

    conn.commit()

    cur.execute("SELECT id, username, role, `system`, department, status FROM users ORDER BY id")
    print("[6/6] final users table:")
    for row in cur.fetchall():
        print("      ", row)

    cur.execute("SELECT COUNT(*) FROM system_users")
    print(f"\nDone. `system_users` left intact ({cur.fetchone()['COUNT(*)']} rows) for rollback; "
          f"snapshot: {backup}")
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="perform the migration")
    parser.add_argument("--database", help="target database (defaults to DB_NAME)")
    args = parser.parse_args()

    conn = connect(args.database)
    try:
        print(f"Database: {args.database or os.getenv('DB_NAME', 'expired')}")
        print(f"Mode: {'APPLY' if args.apply else 'DRY RUN'}\n")
        return apply_migration(conn, dry_run=not args.apply)
    except Exception as exc:
        conn.rollback()
        print(f"\nFAILED, rolled back: {exc}")
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
