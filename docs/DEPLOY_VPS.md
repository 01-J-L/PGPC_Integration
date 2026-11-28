# Updating the PGPC system on the Hostinger VPS

`scripts/deploy_vps.sh` does the whole update and stops at the first problem. It never deletes data.

- It **never overwrites**: `.env`, `website/static/images/uploads/`, the Python `venv`, `.git`.
- It **backs up before changing anything**: the database (`mysqldump`), the code (tar, which also holds `.env` and uploads) and the venv. Backups go to `~/pgpc_backups/<date_time>/`.
- It **rehearses** the 11 database migrations on a copy (`<db>_rehearsal`) before touching the real database.
- **If something fails after the app was stopped**, it puts back the old code and venv and starts the app again by itself.

Tested on a copy of the original database (`full.sql` from the uploaded system), with these cases:
- normal update;
- re-running it;
- a broken release (caught by the rehearsal, and caught again after the stop when the rehearsal is skipped);
- manual undo back to the original version;
- app folder owned by `www-data`.

---

## 0. Connect to the VPS

Use either of these:
- hPanel → VPS → **Browser terminal**, or
- from your PC: `ssh root@YOUR_VPS_IP`

## 1. Find out how the app runs (once)

```bash
python3 --version; lsb_release -ds
systemctl list-units --type=service --all | grep -iE "gunicorn|pgpc|flask|app"
ps aux | grep -E "gunicorn|wsgi.py|main.py|flask" | grep -v grep
```

What to look for in the output:

- **Python 3.10 or newer** is needed. Ubuntu 22.04 has 3.10 and 24.04 has 3.12. On Ubuntu 20.04 (Python 3.8), see *Old Python* below.
- **A service name** (for example `pgpc.service`) means the script will find it by itself.
- **No service, and the app runs as `python wsgi.py`, `python main.py`, inside `screen`/`tmux`, or with `nohup`:** do *First-time service setup* below first. `wsgi.py` starts Werkzeug's debugger, which lets anyone who triggers an error run commands on your server, so it must not be used in production.

## 2. Upload the release zip

From your Windows PC (PowerShell, in the folder with the zip):

```powershell
scp .\PGPC_Integration_vps_update.zip root@YOUR_VPS_IP:/root/
```

FileZilla/WinSCP (SFTP, port 22) to `/root/` works too.

## 3. Check (changes nothing)

```bash
cd ~
unzip -o -j PGPC_Integration_vps_update.zip 'PGPC_Integration/scripts/deploy_vps.sh' -d ~
bash ~/deploy_vps.sh --check ~/PGPC_Integration_vps_update.zip
```

Fix every `WARNING`, then run the check again until it prints **Check passed**.

If it cannot find something, tell it where:

```bash
APP_DIR=/var/www/pgpc SERVICE=pgpc VENV=/var/www/pgpc/venv bash ~/deploy_vps.sh --check ~/PGPC_Integration_vps_update.zip
```

Use the same variables in step 4.

## 4. Update

Pick a quiet time: the app is offline for about a minute.

```bash
bash ~/deploy_vps.sh ~/PGPC_Integration_vps_update.zip
```

Answer `y` when it asks. It ends with **Update finished** and the backup folder.

Then:
1. Log in once as registrar, cashier, accounting and a student.
2. Open their home pages.
3. In the browser, press **Ctrl+F5** so the new CSS/JS loads.

## 5. Undo (only if needed)

```bash
bash ~/deploy_vps.sh --rollback                      # latest backup
bash ~/deploy_vps.sh --rollback ~/pgpc_backups/2026...  # a specific one
```

This puts back the code and the venv. The database keeps the new tables, which the old code ignores; this was tested.

The script also prints the command to restore the database dump. Use it only if you really need to: it discards everything entered after the backup.

---

## If the check shows a warning

| Warning | Fix |
|---|---|
| `mysqldump`/`mysql` missing | `sudo apt-get install -y mariadb-client` (or `mysql-client`) |
| `rsync`/`unzip`/`curl` missing | `sudo apt-get install -y rsync unzip curl` |
| cannot log in to the database | the `DB_*` values in `.env` are wrong for this server |
| disk space | `sudo apt-get clean`; delete old folders in `~/pgpc_backups/` (keep the latest) |
| rehearsal: access denied | allow the app's DB user to make the copy (below), or answer `y` to continue without it (the step 2 backup is still taken) |

Allowing the rehearsal copy. Run once, as the MySQL root user. Replace `pgpc_db` and `pgpc_user` with `DB_NAME` and `DB_USER` from `.env`:

```bash
sudo mysql -e "GRANT ALL ON \`pgpc_db_rehearsal\`.* TO 'pgpc_user'@'localhost'; FLUSH PRIVILEGES;"
```

## Old Python (Ubuntu 20.04: Python 3.8)

The new version needs Python 3.10 or newer (numpy, pandas and mysql-connector require it). Make a new venv with 3.10, then point the script at it:

```bash
sudo add-apt-repository -y ppa:deadsnakes/ppa && sudo apt-get update
sudo apt-get install -y python3.10 python3.10-venv
python3.10 -m venv /var/www/pgpc/venv310
```

Next, edit the service so `ExecStart` uses `/var/www/pgpc/venv310/bin/gunicorn` (see below). Then run the update:

```bash
VENV=/var/www/pgpc/venv310 bash ~/deploy_vps.sh ~/PGPC_Integration_vps_update.zip
```

## First-time service setup (if the app is not a systemd service yet)

Replace `/var/www/pgpc` with your app folder (the one with `main.py` and `.env`):

```bash
sudo tee /etc/systemd/system/pgpc.service >/dev/null <<'EOF'
[Unit]
Description=PGPC web application
After=network.target mariadb.service mysql.service

[Service]
User=root
WorkingDirectory=/var/www/pgpc
ExecStart=/var/www/pgpc/venv/bin/gunicorn --workers 3 --timeout 120 --bind 127.0.0.1:8000 main:app
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
/var/www/pgpc/venv/bin/pip install gunicorn
# stop the old way of running it first (Ctrl+C in its screen/tmux, or: pkill -f wsgi.py)
sudo systemctl daemon-reload && sudo systemctl enable --now pgpc
curl -I http://127.0.0.1:8000/
```

nginx forwards the public site to gunicorn (`/etc/nginx/sites-available/pgpc`, then `ln -s` it into `sites-enabled`):

```nginx
server {
    listen 80;
    server_name your-domain.com;
    client_max_body_size 20M;            # document uploads are up to 5 MB each
    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

```bash
sudo nginx -t && sudo systemctl reload nginx
```

For HTTPS:
1. Run `sudo apt-get install -y certbot python3-certbot-nginx && sudo certbot --nginx -d your-domain.com`.
2. Add `SESSION_COOKIE_SECURE=True` to `.env`.
3. Restart the app: `sudo systemctl restart pgpc`.

## Looking at errors

```bash
sudo journalctl -u pgpc -n 100 --no-pager     # app log
ls -t ~/pgpc_backups | head -1                 # latest backup folder: migrate.txt, rehearsal.txt, audit_after.txt
```
