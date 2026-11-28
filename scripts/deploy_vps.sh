#!/usr/bin/env bash
# =============================================================================
#  PGPC: update the application on a Linux VPS (Hostinger or any Ubuntu/Debian)
#
#  1) Check only (changes nothing):   bash deploy_vps.sh --check  <release.zip>
#  2) Update:                         bash deploy_vps.sh            <release.zip>
#  3) Undo the last update:           bash deploy_vps.sh --rollback [backup-folder]
#
#  Optional settings (environment variables) when they cannot be detected:
#    APP_DIR=/var/www/pgpc      folder with main.py, website/ and .env
#    SERVICE=pgpc               systemd service that runs gunicorn
#    VENV=/var/www/pgpc/venv    Python virtual environment of the app
#    SKIP_REHEARSAL=1           skip the migration rehearsal on a copy of the DB
#
#  What an update does:
#    - checks the server (Python, tools, disk, database, service) BEFORE touching anything
#    - backs up the database (mysqldump), the code (tar) and the Python environment
#    - installs the new Python packages
#    - rehearses the database migrations on a COPY of the database
#    - stops the app, copies the new code (.env, uploads and venv are never overwritten),
#      applies the migrations, checks that the app imports, starts the app, checks it answers
#    - if anything fails after the app was stopped: puts back the old code and Python
#      environment and starts the app again. The database is NOT restored automatically:
#      the migrations only add tables/columns, so the old code keeps working with them.
#      The command to restore the database backup is printed if you ever need it.
# =============================================================================
set -Eeuo pipefail

MODE=deploy
if [[ "${1:-}" == "--check" ]]; then MODE=check; shift; fi
if [[ "${1:-}" == "--rollback" ]]; then MODE=rollback; shift; fi

BOLD=$'\e[1m'; RED=$'\e[31m'; GRN=$'\e[32m'; YEL=$'\e[33m'; RST=$'\e[0m'
step() { echo; echo "${BOLD}==> $*${RST}"; }
ok()   { echo "   ${GRN}OK${RST}  $*"; }
warn() { echo "   ${YEL}WARNING${RST}  $*"; }
die()  { echo; echo "${RED}${BOLD}STOPPED:${RST} $*"; exit 1; }
ask()  { local a; read -r -p "   $1 [y/N] " a; [[ "$a" =~ ^[Yy]$ ]]; }

SUDO=""; [[ $EUID -ne 0 ]] && SUDO="sudo"
TS=$(date +%Y%m%d_%H%M%S)
BACKUP_ROOT="${BACKUP_ROOT:-$HOME/pgpc_backups}"

# ----------------------------------------------------------------------------- detection
detect_app_dir() {
    [[ -n "${APP_DIR:-}" ]] && return
    local pid cwd
    for pid in $(pgrep -f "gunicorn.*main:app" 2>/dev/null || true) $(pgrep -f "python.*(main|wsgi)\.py" 2>/dev/null || true); do
        cwd=$(readlink -f "/proc/$pid/cwd" 2>/dev/null || true)
        if [[ -n "$cwd" && -f "$cwd/main.py" && -d "$cwd/website" ]]; then APP_DIR="$cwd"; return; fi
    done
    for cwd in /var/www/* /home/*/* /root/* /opt/*; do
        if [[ -f "$cwd/main.py" && -d "$cwd/website" && -f "$cwd/.env" ]]; then APP_DIR="$cwd"; return; fi
    done
}

detect_service() {
    [[ -n "${SERVICE:-}" ]] && return
    local f
    for f in /etc/systemd/system/*.service /lib/systemd/system/*.service; do
        [[ -f "$f" ]] || continue
        if grep -qE "WorkingDirectory=${APP_DIR}/?\s*$" "$f" 2>/dev/null || grep -qE "ExecStart=.*${APP_DIR}/" "$f" 2>/dev/null; then
            SERVICE=$(basename "$f" .service); return
        fi
    done
}

detect_venv() {
    [[ -n "${VENV:-}" ]] && return
    local v
    for v in "$APP_DIR/venv" "$APP_DIR/.venv" "$APP_DIR/env"; do
        [[ -x "$v/bin/python" ]] && { VENV="$v"; return; }
    done
    if [[ -n "${SERVICE:-}" ]]; then
        v=$(systemctl cat "$SERVICE" 2>/dev/null | grep -oE "ExecStart=\S+" | sed 's/ExecStart=//; s#/bin/[^/]*$##' | head -1)
        [[ -n "$v" && -x "$v/bin/python" ]] && VENV="$v"
    fi
}

write_mysql_cnf() {   # database credentials from .env into a private option file (never on the command line)
    MYCNF=$(mktemp); chmod 600 "$MYCNF"
    "$VENV/bin/python" - "$APP_DIR/.env" "$MYCNF" <<'PY'
import sys
from dotenv import dotenv_values
v = dotenv_values(sys.argv[1])
with open(sys.argv[2], 'w') as f:
    f.write('[client]\n')
    f.write('host=%s\n' % (v.get('DB_HOST') or 'localhost'))
    f.write('port=%s\n' % (v.get('DB_PORT') or '3306'))
    f.write('user=%s\n' % (v.get('DB_USER') or 'root'))
    f.write('password="%s"\n' % (v.get('DB_PASSWORD') or '').replace('\\', '\\\\').replace('"', '\\"'))
print(v.get('DB_NAME') or '', file=sys.stderr)
PY
    DB_NAME=$("$VENV/bin/python" -c "from dotenv import dotenv_values; print(dotenv_values('$APP_DIR/.env').get('DB_NAME') or '')")
}
cleanup() { [[ -n "${MYCNF:-}" && -f "$MYCNF" ]] && rm -f "$MYCNF"; [[ -n "${REL_TMP:-}" && -d "$REL_TMP" ]] && rm -rf "$REL_TMP"; return 0; }
trap cleanup EXIT

app_port_check() {   # does the app answer? 200/301/302/303 are fine
    local bind code
    bind=$(systemctl cat "$SERVICE" 2>/dev/null | grep -oE -- "(--bind|-b)[ =]+\S+" | head -1 | sed -E 's/(--bind|-b)[ =]+//')
    bind=${bind:-127.0.0.1:8000}
    for _ in $(seq 1 20); do
        if [[ "$bind" == unix:* ]]; then
            code=$(curl -s -o /dev/null -w "%{http_code}" --unix-socket "${bind#unix:}" http://localhost/ || true)
        else
            code=$(curl -s -o /dev/null -w "%{http_code}" "http://${bind/0.0.0.0/127.0.0.1}/" || true)
        fi
        [[ "$code" =~ ^(200|301|302|303)$ ]] && { ok "the app answers on $bind (HTTP $code)"; return 0; }
        sleep 2
    done
    echo "   the app did not answer on $bind (last HTTP code: ${code:-none})"; return 1
}

service_stop()  { $SUDO systemctl stop "$SERVICE"; }
service_start() { $SUDO systemctl start "$SERVICE"; sleep 3; $SUDO systemctl is-active --quiet "$SERVICE"; }

# ----------------------------------------------------------------------------- rollback
restore_previous() {   # $1 = backup folder
    local b="$1"
    [[ -f "$b/code.tgz" ]] || die "No code backup in $b"
    step "Putting back the previous version from $b"
    service_stop || true
    tar -xzf "$b/code.tgz" -C "$APP_DIR"
    ok "code restored"
    if [[ -d "$b/venv" ]]; then
        rm -rf "$VENV" && cp -a "$b/venv" "$VENV"
        ok "Python environment restored"
    fi
    if service_start; then ok "service $SERVICE started"; else warn "service $SERVICE did not start: sudo journalctl -u $SERVICE -n 80"; fi
    echo
    echo "   The database was left as it is (the old code works with the added tables)."
    echo "   Only if you really need the database of before the update:"
    echo "     sudo systemctl stop $SERVICE"
    echo "     mysql --defaults-extra-file=<file with your DB login> $DB_NAME < $b/database.sql"
    echo "   (this loses everything entered after the backup was taken)."
}

# ============================================================================= start
detect_app_dir
[[ -n "${APP_DIR:-}" ]] || die "Could not find the application folder. Run again with APP_DIR=/path/to/app (the folder with main.py and .env)."
APP_DIR=$(readlink -f "$APP_DIR")
[[ -f "$APP_DIR/main.py" && -d "$APP_DIR/website" ]] || die "$APP_DIR does not look like the PGPC application (no main.py / website/)."
[[ -f "$APP_DIR/.env" ]] || die "$APP_DIR/.env is missing: the live settings must stay on the server."
detect_service
detect_venv
[[ -n "${VENV:-}" ]] || die "Could not find the Python virtual environment. Run again with VENV=/path/to/venv."
write_mysql_cnf 2>/dev/null
[[ -n "$DB_NAME" ]] || die "DB_NAME is not set in $APP_DIR/.env"

if [[ "$MODE" == rollback ]]; then
    [[ -n "${SERVICE:-}" ]] || die "Could not find the systemd service. Run again with SERVICE=<name>."
    B="${1:-$(ls -1dt "$BACKUP_ROOT"/*/ 2>/dev/null | head -1)}"
    [[ -n "$B" && -d "$B" ]] || die "No backup folder found in $BACKUP_ROOT"
    echo "App: $APP_DIR   Service: $SERVICE   Backup: $B"
    ask "Put back the code and Python environment from this backup?" || die "Nothing changed."
    restore_previous "${B%/}"
    app_port_check || true
    exit 0
fi

ZIP="${1:-}"
[[ -n "$ZIP" && -f "$ZIP" ]] || die "Give the release zip, e.g.: bash deploy_vps.sh --check ~/PGPC_Integration_fee_descriptions.zip"
ZIP=$(readlink -f "$ZIP")

# ----------------------------------------------------------------------------- 1. checks
step "1/8  Checking the server (nothing is changed in this step)"
echo "   App folder : $APP_DIR"
echo "   Service    : ${SERVICE:-<not found>}"
echo "   Python env : $VENV"
echo "   Database   : $DB_NAME"
PROBLEMS=0
PYV=$("$VENV/bin/python" -c 'import sys; print("%d.%d" % sys.version_info[:2])')
if "$VENV/bin/python" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then ok "Python $PYV"
else warn "Python $PYV is too old: the application needs Python 3.10 or newer."; PROBLEMS=1; fi
for t in mysqldump mysql unzip rsync curl tar; do
    if command -v "$t" >/dev/null; then ok "$t found"; else warn "$t is missing: sudo apt-get install -y ${t/mysqldump/mariadb-client}"; PROBLEMS=1; fi
done
if [[ -n "${SERVICE:-}" ]]; then
    ok "systemd service: $SERVICE ($(systemctl is-active "$SERVICE" 2>/dev/null || true))"
    if systemctl cat "$SERVICE" 2>/dev/null | grep -E "ExecStart=.*(wsgi\.py|main\.py)" >/dev/null; then
        warn "The service starts the app with 'python wsgi.py/main.py' (development server with the debugger). Use gunicorn: see docs/DEPLOY_VPS.md."
    fi
else
    warn "No systemd service found for $APP_DIR. Run again with SERVICE=<name>, or create one: see docs/DEPLOY_VPS.md."; PROBLEMS=1
fi
if mysql --defaults-extra-file="$MYCNF" -N -e "SELECT 1" "$DB_NAME" >/dev/null 2>&1; then ok "database login works"
else warn "cannot log in to database $DB_NAME with the settings of .env"; PROBLEMS=1; fi
DB_MB=$(mysql --defaults-extra-file="$MYCNF" -N -e "SELECT COALESCE(ROUND(SUM(data_length+index_length)/1024/1024),0) FROM information_schema.tables WHERE table_schema='$DB_NAME'" 2>/dev/null || echo 0)
FREE_MB=$(df -Pm "$HOME" | awk 'NR==2{print $4}')
NEED_MB=$(( DB_MB * 3 + 1500 ))
if (( FREE_MB > NEED_MB )); then ok "disk space: ${FREE_MB} MB free (database ${DB_MB} MB, need ~${NEED_MB} MB)"
else warn "disk space: ${FREE_MB} MB free, about ${NEED_MB} MB needed for backups and the rehearsal"; PROBLEMS=1; fi
if unzip -Z1 "$ZIP" 2>/dev/null | grep -c "website/schema_migrations.py$" >/dev/null; then ok "release zip looks complete"
else warn "$ZIP is not a PGPC release (no website/schema_migrations.py)"; PROBLEMS=1; fi
REL_TMP=$(mktemp -d)
unzip -q "$ZIP" -d "$REL_TMP"
REL=$(dirname "$(find "$REL_TMP" -maxdepth 3 -name main.py -path '*/main.py' | head -1)")
[[ -f "$REL/website/schema_migrations.py" ]] || die "Could not find the application inside $ZIP"
if grep -qiE "^SESSION_COOKIE_SECURE=(true|1)" "$APP_DIR/.env"; then ok "SESSION_COOKIE_SECURE is on"
else echo "   note: if the site is served over https://, add SESSION_COOKIE_SECURE=True to .env (optional)"; fi
echo "   Pending database changes:"
cp "$APP_DIR/.env" "$REL/.env"; chmod 600 "$REL/.env"
( cd "$REL" && "$VENV/bin/python" scripts/migrate.py status 2>/dev/null | sed 's/^/     /' ) || warn "could not read the migration status yet (new packages not installed); this is checked again in step 3"

if [[ "$MODE" == check ]]; then
    echo
    if (( PROBLEMS )); then die "Fix the warnings above, then run the check again."; fi
    echo "${GRN}${BOLD}Check passed.${RST} Nothing was changed. To update: bash $0 $ZIP"
    exit 0
fi
(( PROBLEMS )) && die "Fix the warnings above first (run with --check to see them again)."
echo
ask "Start the update now? The app will be offline for about a minute during step 6." || die "Nothing changed."

# ----------------------------------------------------------------------------- 2. backups
step "2/8  Backups"
B="$BACKUP_ROOT/$TS"; mkdir -p "$B"; chmod 700 "$BACKUP_ROOT" "$B"
mysqldump --defaults-extra-file="$MYCNF" --single-transaction --routines --events --triggers "$DB_NAME" > "$B/database.sql"
tail -1 "$B/database.sql" | grep "Dump completed" >/dev/null || die "The database backup is incomplete ($B/database.sql). Nothing else was changed."
ok "database  -> $B/database.sql ($(du -h "$B/database.sql" | cut -f1))"
tar -czf "$B/code.tgz" -C "$APP_DIR" --exclude="./$(basename "$VENV")" --exclude='__pycache__' --exclude='*.pyc' .
ok "code      -> $B/code.tgz (includes .env and uploaded files)"
cp -a "$VENV" "$B/venv"
ok "Python env-> $B/venv"
echo "$APP_DIR|${SERVICE}|$VENV|$DB_NAME" > "$B/info.txt"

# ----------------------------------------------------------------------------- 3. packages
step "3/8  Installing the Python packages of the new version (the app keeps running)"
if ! "$VENV/bin/pip" install --disable-pip-version-check -q -r "$REL/requirements.txt"; then
    rm -rf "$VENV"; cp -a "$B/venv" "$VENV"
    die "Package installation failed; the Python environment was put back. The app was not touched."
fi
ok "packages installed"
# Build the new app WITHOUT a database (port 1 is never open): the startup create_all must
# not touch the live database before the migrations have run.
load_check() { ( cd "$REL" && DB_HOST=127.0.0.1 DB_PORT=1 "$VENV/bin/python" -c "import main" ); }
load_check >/dev/null 2>&1 || {
    rm -rf "$VENV"; cp -a "$B/venv" "$VENV"
    load_check 2>&1 | grep -v "^\[WARN\]" | tail -15 || true
    die "The new version does not start with these packages (see the error above); the Python environment was put back."
}
ok "the new version loads"

# ----------------------------------------------------------------------------- 4. rehearsal
step "4/8  Rehearsing the database changes on a copy (${DB_NAME}_rehearsal)"
if [[ "${SKIP_REHEARSAL:-0}" == 1 ]]; then
    warn "skipped (SKIP_REHEARSAL=1)"
elif ( cd "$REL" && "$VENV/bin/python" scripts/rehearse_migrations.py --source "$DB_NAME" ) > "$B/rehearsal.txt" 2>&1; then
    grep -E "applied|RESULT" "$B/rehearsal.txt" | sed 's/^/   /'
else
    sed 's/^/   /' "$B/rehearsal.txt" | tail -15
    if grep -iE "denied|access" "$B/rehearsal.txt" >/dev/null; then
        warn "the database user may not create a copy database (no CREATE privilege)."
        ask "Continue without the rehearsal? (the backup of step 2 is your safety net)" || die "Stopped before any change to the app."
    else
        die "The rehearsal failed: the update was NOT applied. Send $B/rehearsal.txt to the developer."
    fi
fi

# ----------------------------------------------------------------------------- 5-8. switch
SWITCHED=0
on_error() {
    local line=$1
    echo; echo "${RED}${BOLD}A step failed (line $line).${RST}"
    if (( SWITCHED )); then restore_previous "$B"; app_port_check || true; fi
    echo; echo "Backup folder: $B"
    exit 1
}
trap 'on_error $LINENO' ERR

step "5/8  Stopping the app"
service_stop
SWITCHED=1
ok "service $SERVICE stopped"

step "6/8  Copying the new code (.env, venv, uploads and .git are kept as they are)"
CHOWN=(); [[ $EUID -eq 0 ]] && CHOWN=(--chown="$(stat -c %U:%G "$APP_DIR")")   # new files get the app folder's owner
rsync -a "${CHOWN[@]}" --exclude='.env' --exclude='venv/' --exclude='.venv/' --exclude="$(basename "$VENV")/" --exclude='.git/' \
      --exclude='__pycache__/' --exclude='*.pyc' --exclude='website/static/images/uploads/' "$REL/" "$APP_DIR/"
find "$APP_DIR" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
ok "code copied"

step "7/8  Applying the database changes"
if ! ( cd "$APP_DIR" && "$VENV/bin/python" scripts/migrate.py apply --backup-done ) > "$B/migrate.txt" 2>&1; then
    tail -20 "$B/migrate.txt" | sed 's/^/   /'; echo "   full log: $B/migrate.txt"; false
fi
grep -E "^\[OK\]|Nothing to apply" "$B/migrate.txt" | sed 's/^/   /' || true
if ( cd "$APP_DIR" && "$VENV/bin/python" scripts/migrate.py status ) | grep PENDING >/dev/null; then
    echo "   a migration is still pending"; false
fi
ok "database is up to date"
( cd "$APP_DIR" && "$VENV/bin/python" scripts/audit_data.py ) > "$B/audit_after.txt" 2>&1 || true
ok "data audit -> $B/audit_after.txt"

step "8/8  Starting the app"
service_start
ok "service $SERVICE is running"
app_port_check
if $SUDO journalctl -u "$SERVICE" --since "-2 min" --no-pager 2>/dev/null | grep "Traceback" >/dev/null; then
    warn "errors in the log since the start: sudo journalctl -u $SERVICE -n 100"
fi
trap - ERR

echo
echo "${GRN}${BOLD}Update finished.${RST}"
echo "   Backups      : $B"
echo "   Undo         : bash $0 --rollback $B"
echo "   Then log in once as registrar, cashier, accounting and a student, and open their home pages."
