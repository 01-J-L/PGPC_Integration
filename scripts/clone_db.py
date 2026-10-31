"""Clone the live schema+data into a scratch database so migrations can be
tested without touching production data.

    py scripts/clone_db.py expired_phase3_test
    py scripts/clone_db.py expired_phase3_test --drop
"""

import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from dotenv import load_dotenv

load_dotenv(os.path.join(ROOT, ".env"))

import mysql.connector

SOURCE = os.getenv("DB_NAME", "expired")


def connect(database=None):
    return mysql.connector.connect(
        host=os.getenv("DB_HOST", "localhost"),
        port=int(os.getenv("DB_PORT", "3306")),
        user=os.getenv("DB_USER", "root"),
        password=os.getenv("DB_PASSWORD", ""),
        database=database,
        autocommit=True,
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("target")
    parser.add_argument("--drop", action="store_true", help="drop target first")
    args = parser.parse_args()

    conn = connect()
    cur = conn.cursor()

    if args.drop:
        cur.execute(f"DROP DATABASE IF EXISTS `{args.target}`")
    cur.execute(
        f"CREATE DATABASE IF NOT EXISTS `{args.target}` "
        f"CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
    )

    cur.execute(
        "SELECT TABLE_NAME FROM information_schema.TABLES "
        "WHERE TABLE_SCHEMA = %s AND TABLE_TYPE = 'BASE TABLE'",
        (SOURCE,),
    )
    tables = [r[0] for r in cur.fetchall()]

    cur.execute("SET FOREIGN_KEY_CHECKS = 0")
    for table in tables:
        cur.execute(f"DROP TABLE IF EXISTS `{args.target}`.`{table}`")
        cur.execute(f"CREATE TABLE `{args.target}`.`{table}` LIKE `{SOURCE}`.`{table}`")
        cur.execute(f"INSERT INTO `{args.target}`.`{table}` SELECT * FROM `{SOURCE}`.`{table}`")
    cur.execute("SET FOREIGN_KEY_CHECKS = 1")

    cur.execute(
        "SELECT TABLE_NAME, TABLE_ROWS FROM information_schema.TABLES "
        "WHERE TABLE_SCHEMA = %s AND TABLE_TYPE = 'BASE TABLE' ORDER BY TABLE_NAME",
        (args.target,),
    )
    copied = cur.fetchall()
    total = sum((r[1] or 0) for r in copied)
    print(f"Cloned {len(tables)} tables from `{SOURCE}` into `{args.target}` (~{total} rows estimated)")

    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
