# systemd units for Taey-Ed (Mira)

Three units that keep the taey-ed central stack alive across reboot.

| Unit | What | Port | Logs |
|---|---|---|---|
| `cloudflared-taey-ed.service` | Cloudflare Tunnel (taey-ed-api.taey.ai → :5003 + the other taey.ai routes in `~/.cloudflared/config.yml`) | — | `/home/user/taey-ed/logs/cloudflared.log` |
| `taey-ed-api.service` | Spark API (FastAPI/uvicorn) | 5003 | `/home/user/taey-ed/logs/api.log` |
| `taey-ed-worker.service` | Consultation worker (headless Claude CLI → BT generator). Polls `/tmp/taey-ed-consult/` every 2s | — | `/home/user/taey-ed/logs/worker.log` |

## Install

```bash
sudo cp deploy/systemd/*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now cloudflared-taey-ed.service taey-ed-api.service taey-ed-worker.service
```

## Status

```bash
systemctl status taey-ed-api.service taey-ed-worker.service cloudflared-taey-ed.service
```

## Common operations

```bash
# Restart the API after a code change
sudo systemctl restart taey-ed-api.service

# Restart the worker after editing prompt_codex.py / bt_generator.py
sudo systemctl restart taey-ed-worker.service

# Reload the tunnel after editing ~/.cloudflared/config.yml (do this, not SIGHUP — SIGHUP kills cloudflared in this build)
sudo systemctl restart cloudflared-taey-ed.service

# Tail live logs
journalctl -u taey-ed-api.service -f
```

## Notes

- All three run as user `mira` so the venv at `/home/user/taey-ed/.venv` and the OAuth state under `/home/user/.claude/` are reachable.
- The worker needs `HOME=/home/user` set explicitly so the headless `claude` CLI finds its Max-subscription OAuth state.
- API takes `TAEY_ED_USE_WORKER=1` so it skips the legacy tmux-notify path and lets the worker pick up consults from disk.
- Production mode is controlled by `TAEY_ED_PRODUCTION=1` on the API unit; leave it unset only for local/dev runs that tolerate ephemeral secrets.
