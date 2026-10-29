# Serving JohnCad through an frp tunnel

Use this when the school Pi (running JohnCad) sits on a network that can't accept
inbound connections. A home Pi with a public IP runs `frps` + Caddy; the school
Pi runs `frpc` and dials out to it.

```
browser --HTTPS--> home Pi (Caddy :443 -> frps :8080) <==tunnel :7000== school Pi (frpc -> gunicorn 127.0.0.1:8000)
```

TLS ends at Caddy on the home Pi, so the school Pi needs no certificate and no
nginx/certbot: point frpc straight at gunicorn.

## Home Pi

1. Install frps to `/opt/frp/`, copy `frps.toml.example` to `/opt/frp/frps.toml` and set a
   long token (`openssl rand -hex 24`). Run it as a systemd service with `Restart=always`.
2. Install Caddy and use `Caddyfile.example` as `/etc/caddy/Caddyfile`.
3. Router: forward TCP **80, 443, 7000** to the home Pi. Do not forward 8080.
4. DNS: A record for `riponhighschoolprint.com` -> home public IP (keep it updated with a
   DDNS client, e.g. qdm12/ddns-updater, record **not** proxied).

## School Pi

1. Install frpc to `/opt/frp/`, copy `frpc.toml.example` to `/opt/frp/frpc.toml` and use the
   same token.
2. `sudo cp deploy/frp/frpc.service /etc/systemd/system/ && sudo systemctl enable --now frpc`
3. Look for `login to server success` in `journalctl -u frpc`.

## JohnCad `.env` on the school Pi

```
DEBUG=False
ALLOWED_HOSTS=riponhighschoolprint.com,localhost
CSRF_TRUSTED_ORIGINS=https://riponhighschoolprint.com
HTTPS_ENABLED=True
```

`HTTPS_ENABLED=True` is correct even though gunicorn only sees HTTP: browsers reach the
site over HTTPS (Caddy), so session/CSRF cookies should be `Secure`. Then
`sudo systemctl restart johncad`.

## Bambuddy

Bambuddy runs on the school Pi next to JohnCad and talks to the printers and to
gunicorn locally (`127.0.0.1`), so none of that traffic goes through the tunnel. The
Caddyfile returns 404 for `/api/*` so Bambuddy's webhook endpoint stays private. See the
main README's "Bambuddy setup" and `docs/BAMBUDDY_PLAN.md`.

## Troubleshooting

| Symptom | Cause |
|---|---|
| 404 from frps | frpc not connected, or `customDomains` doesn't match the Host |
| 400 Bad Request | domain missing from `ALLOWED_HOSTS` |
| 403 CSRF failed | `CSRF_TRUSTED_ORIGINS` missing or wrong scheme |
| Upload fails on large `.stl` | check Caddy `max_size`, gunicorn `--timeout`, and the home Pi's upload bandwidth |
