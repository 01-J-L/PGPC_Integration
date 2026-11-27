"""Versioned, idempotent schema migrations for an EXISTING PGPC database.

Why this exists
---------------
`db.create_all()` (run at start-up) only creates *missing tables*; it never
adds a column to a table that already exists. Flask-Migrate is listed in
requirements.txt but was never initialised, and the production database was
built from full.sql, so an Alembic baseline would first have to be stamped.
Until that is done, schema changes are shipped here: each migration is small,
additive, safe to re-run, and recorded in the `schema_migrations` table.

Use
---
    python scripts/migrate.py status          # what is applied / pending
    python scripts/migrate.py apply           # take a backup first (see docs)

The application never applies migrations by itself; at start-up it only logs
a warning when migrations are pending.
"""

import datetime

# A migration has either `steps`, a list of (check_sql, check_params, apply_sql)
# where `check_sql` returns a row when the step is ALREADY done, or `build`, a
# function that inspects the live schema and returns only the SQL still needed.
# Either way re-running a migration is a no-op.
_COLUMN_EXISTS = ("SELECT 1 FROM information_schema.COLUMNS "
                  "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s AND COLUMN_NAME = %s")


def _add_column(table, column, ddl):
    return (_COLUMN_EXISTS, (table, column), f"ALTER TABLE `{table}` ADD COLUMN `{column}` {ddl}")


_BLOB_AT_LEAST_MEDIUM = ("SELECT 1 FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = DATABASE() "
                         "AND TABLE_NAME = %s AND COLUMN_NAME = %s AND DATA_TYPE IN ('mediumblob', 'longblob')")

# Every uploaded-file column on `applicants` (uploads are capped at 5 MB in
# process_uploaded_file(); MEDIUMBLOB holds up to 16 MB).
DOCUMENT_BLOB_COLUMNS = [
    'photo', 'shs_diploma_file', 'shs_card_file', 'birth_certificate_file',
    'enrollment_good_moral_file', 'enrollment_psa_birth_file', 'enrollment_photos_2x2_file',
    'enrollment_entrance_fee_proof_file', 'enrollment_voters_id_file', 'enrollment_cbs_file',
    'enrollment_brgy_cert_file', 'pwd_id_file',
    'enrollment_shs_card_file', 'enrollment_shs_diploma_file',
]


def _widen_document_blobs(cur):
    """One ALTER for every still-narrow column, so the (large) applicants table
    is rebuilt once instead of once per column."""
    narrow = []
    for col in DOCUMENT_BLOB_COLUMNS:
        cur.execute(_COLUMN_EXISTS, ('applicants', col))
        exists = cur.fetchone()
        cur.execute(_BLOB_AT_LEAST_MEDIUM, ('applicants', col))
        if exists and not cur.fetchone():
            narrow.append(col)
    if not narrow:
        return []
    return ["ALTER TABLE `applicants` " + ", ".join(f"MODIFY `{c}` MEDIUMBLOB NULL" for c in narrow)]


_TABLE_EXISTS = ("SELECT 1 FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s")


def model_table_ddl(table_names):
    """CREATE TABLE statements generated from website/models.py, so the model
    stays the single source of truth for new tables."""
    from sqlalchemy.schema import CreateTable, CreateIndex
    from sqlalchemy.dialects import mysql
    from website import db
    from website import models  # noqa: F401  (registers every table)
    out = []
    for name in table_names:
        table = db.metadata.tables[name]
        ddl = str(CreateTable(table).compile(dialect=mysql.dialect())).strip()
        ddl = ddl.replace('CREATE TABLE', 'CREATE TABLE IF NOT EXISTS', 1)
        out.append((name, ddl, [str(CreateIndex(ix).compile(dialect=mysql.dialect())).strip() for ix in table.indexes]))
    return out


def _create_model_tables(table_names):
    def build(cur):
        # New tables reference programs.program_id / student_users.id; a FOREIGN
        # KEY on a VARCHAR needs the same charset+collation, so copy it from the
        # live programs table instead of trusting the server default.
        cur.execute("SELECT CHARACTER_SET_NAME, COLLATION_NAME FROM information_schema.COLUMNS "
                    "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'programs' AND COLUMN_NAME = 'program_id'")
        row = cur.fetchone()
        options = (f" ENGINE=InnoDB DEFAULT CHARSET={row[0]} COLLATE={row[1]}" if row and row[0]
                   else " ENGINE=InnoDB")
        statements = []
        for name, ddl, index_ddl in model_table_ddl(table_names):
            cur.execute(_TABLE_EXISTS, (name,))
            if cur.fetchone():
                continue
            # Replace the model's table options with the live database's.
            statements.append(ddl[:ddl.rstrip().rfind(')') + 1] + options)
            statements.extend(index_ddl)
        return statements
    return build


MIGRATIONS = [
    {
        'id': '0001_enrollment_shs_document_columns',
        'description': ("P1-03: enrollment copies of Form 138 (SHS card) and SHS diploma. "
                        "The registrar upload and document viewer already wrote/read these "
                        "columns, which did not exist, so both actions crashed."),
        'steps': [
            _add_column('applicants', 'enrollment_shs_card_file', 'MEDIUMBLOB NULL'),
            _add_column('applicants', 'enrollment_shs_card_filename', 'VARCHAR(255) NULL'),
            _add_column('applicants', 'enrollment_shs_card_mimetype', 'VARCHAR(100) NULL'),
            _add_column('applicants', 'enrollment_shs_diploma_file', 'MEDIUMBLOB NULL'),
            _add_column('applicants', 'enrollment_shs_diploma_filename', 'VARCHAR(255) NULL'),
            _add_column('applicants', 'enrollment_shs_diploma_mimetype', 'VARCHAR(100) NULL'),
        ],
        'rollback': [
            "ALTER TABLE `applicants` DROP COLUMN `enrollment_shs_card_file`, "
            "DROP COLUMN `enrollment_shs_card_filename`, DROP COLUMN `enrollment_shs_card_mimetype`, "
            "DROP COLUMN `enrollment_shs_diploma_file`, DROP COLUMN `enrollment_shs_diploma_filename`, "
            "DROP COLUMN `enrollment_shs_diploma_mimetype`",
        ],
    },
    {
        'id': '0002_document_columns_mediumblob',
        'description': ("P1-12: document/photo columns were BLOB (64 KB max) while uploads allow 5 MB, "
                        "so normal scans failed with 'Data too long'. Widen to MEDIUMBLOB (no data change). "
                        "Rebuilds the applicants table: run in a quiet period."),
        'build': _widen_document_blobs,
        # Narrowing back fails (strict mode) if any file is now larger than 64 KB,
        # which is the safe outcome; restore from the backup instead in that case.
        'rollback': ["ALTER TABLE `applicants` " + ", ".join(
            f"MODIFY `{c}` BLOB NULL" for c in DOCUMENT_BLOB_COLUMNS)],
    },
    {
        'id': '0003_curriculum_management',
        'description': ("Phase 2: versioned curricula (curriculum_versions, curriculum_subjects, "
                        "curriculum_prerequisites), student curriculum assignment (student_curricula), "
                        "staged reference imports (curriculum_import_batches) and curriculum_audit_log. "
                        "New tables only; no existing table is changed."),
        'build': _create_model_tables(['curriculum_versions', 'curriculum_subjects', 'curriculum_prerequisites',
                                       'student_curricula', 'curriculum_import_batches', 'curriculum_audit_log']),
        # Refuse to drop tables that already hold curriculum data (use --force after exporting).
        'rollback_guard': "SELECT (SELECT COUNT(*) FROM curriculum_versions) + (SELECT COUNT(*) FROM student_curricula)",
        'rollback': [
            "DROP TABLE IF EXISTS `curriculum_audit_log`",
            "DROP TABLE IF EXISTS `curriculum_import_batches`",
            "DROP TABLE IF EXISTS `student_curricula`",
            "DROP TABLE IF EXISTS `curriculum_prerequisites`",
            "DROP TABLE IF EXISTS `curriculum_subjects`",
            "DROP TABLE IF EXISTS `curriculum_versions`",
        ],
    },
]

_LEDGER_DDL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    id VARCHAR(100) NOT NULL PRIMARY KEY,
    description VARCHAR(500),
    applied_at DATETIME NOT NULL
) DEFAULT CHARSET=utf8mb4
"""


def _ensure_ledger(cur):
    cur.execute(_LEDGER_DDL)


def applied_ids(conn):
    cur = conn.cursor()
    _ensure_ledger(cur)
    cur.execute("SELECT id FROM schema_migrations")
    ids = {r[0] for r in cur.fetchall()}
    cur.close()
    return ids


def pending(conn):
    done = applied_ids(conn)
    return [m for m in MIGRATIONS if m['id'] not in done]


def apply_all(conn, log=print):
    """Apply every pending migration. DDL auto-commits in MySQL, so each step is
    checked before it runs; a re-run after a failure resumes where it stopped."""
    applied = []
    for m in pending(conn):
        cur = conn.cursor()
        for check_sql, params, apply_sql in m.get('steps', []):
            cur.execute(check_sql, params)
            if cur.fetchone():
                log(f"  [skip] {apply_sql}")
                continue
            log(f"  [run ] {apply_sql}")
            cur.execute(apply_sql)
        if 'build' in m:
            # `build(cur)` inspects the live schema and returns only what is still needed.
            statements = m['build'](cur)
            if not statements:
                log("  [skip] nothing left to change")
            for sql in statements:
                log(f"  [run ] {sql}")
                cur.execute(sql)
        cur.execute("INSERT INTO schema_migrations (id, description, applied_at) VALUES (%s, %s, %s)",
                    (m['id'], m['description'][:500], datetime.datetime.now()))
        conn.commit()
        cur.close()
        applied.append(m['id'])
        log(f"[OK] {m['id']}")
    return applied
