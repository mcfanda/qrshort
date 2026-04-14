# QR Short URL

A small FastAPI app for creating short URLs and QR codes.

This first server version is configured for:

```text
BASE_URL=http://127.0.0.1:8002
```

Later, when the domain is ready, change `BASE_URL` and wire nginx to the FQDN.

The admin UI and link creation API require a login. Redirects and QR images remain public.

Each short URL has a nickname. Use the saved QR codes page to find existing short URLs and download their QR images again:

```text
http://127.0.0.1:8002/links
```

Saved links can also be deleted from that page.

## Run Manually

```bash
cd /opt/qrshort
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
uvicorn app.main:app --host 127.0.0.1 --port 8002
```

Open this from the server:

```text
http://127.0.0.1:8002
```

## PostgreSQL

Example database setup:

```bash
sudo -u postgres psql
CREATE USER qrshort WITH PASSWORD 'change-me';
CREATE DATABASE qrshort OWNER qrshort;
\q
```

The app creates its table on startup from `schema.sql`.

## Authentication

The server reads these values from `/etc/qrshort.env`:

```bash
APP_ADMIN_PASSWORD=change-me
APP_SECRET_KEY=change-me-to-a-long-random-string
```

After changing either value, restart the service:

```bash
systemctl restart qrshort
```

## systemd

After the virtual environment and `/etc/qrshort.env` are ready:

```bash
cp deploy/qrshort.service /etc/systemd/system/qrshort.service
systemctl daemon-reload
systemctl enable --now qrshort
```

Check it:

```bash
systemctl status qrshort
curl http://127.0.0.1:8002/health
```

## API

Create a link:

```bash
curl -X POST http://127.0.0.1:8002/api/links \
  -H 'Content-Type: application/json' \
  -d '{"nickname":"Example","url":"https://example.com","custom_slug":"example"}'
```

Short URL:

```text
http://127.0.0.1:8002/u/example
```

QR code:

```text
http://127.0.0.1:8002/qr/example.png
```
