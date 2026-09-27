# Running the Noadcast server under systemd

The server runs as a systemd **user** unit on `laurel`, bound to loopback and
the tailnet address (never `0.0.0.0`). Nothing here is installed
automatically; run these commands once, as the user that owns the checkout.

## Storage on laurel

Bulk runtime storage lives on the SSD pool. The checkout uses these symlinks:

```
/home/ikhor/code/noadcast/data   -> /mnt/ssd-pool/services/noadcast/data
/home/ikhor/code/noadcast/.cache -> /mnt/ssd-pool/services/noadcast/cache
```

This keeps the database, downloaded audio, model files, classifier responses,
temporary files, and dependency/model caches on the pool while preserving the
paths used by the service and CLI. Stop the service before relocating either
directory. The API token and provider keys remain in `secrets.env` (mode 600).

## First-time setup

```bash
cd /home/ikhor/code/noadcast
cp secrets.env.example secrets.env && chmod 600 secrets.env
.venv/bin/noadcast token            # paste into NOADCAST_API_TOKEN in secrets.env
$EDITOR secrets.env                 # NOADCAST_HOST=127.0.0.1,100.80.188.91, API keys
.venv/bin/noadcast models link benchmarks/tal/models/tiny.en   # or: noadcast models fetch
.venv/bin/noadcast models verify
.venv/bin/noadcast migrate
.venv/bin/noadcast pool-selftest    # optional: transcribes one corpus episode
```

## Install and enable

```bash
mkdir -p ~/.config/systemd/user
ln -sf /home/ikhor/code/noadcast/deploy/noadcast.service ~/.config/systemd/user/noadcast.service
systemctl --user daemon-reload
systemctl --user enable --now noadcast.service
sudo loginctl enable-linger "$USER"   # keep user services running without a login session
```

`systemctl --user daemon-reload` again after editing the unit.

## Operate

Deploy source changes through Git: commit and push from the development
checkout, then run `git pull --ff-only` in the checkout on laurel. Do not
copy working-tree files with archives or file-transfer tools. Preserve any
unrelated remote work before updating. For dependency changes, use
`uv sync --locked --inexact` with `UV_CACHE_DIR` under the project `.cache`;
the inexact mode preserves extra packages used by local benchmarks.

```bash
systemctl --user status noadcast
systemctl --user restart noadcast            # SIGTERM: drains jobs, then joins the workers (<= 90 s)
journalctl --user -u noadcast -f             # JSON lines, one per event
journalctl --user -u noadcast -o cat | jq -c 'select(.episode_id == 42)'   # one episode end to end
.venv/bin/noadcast status                    # queues, disk, spend, failures (reads the database)
.venv/bin/noadcast refresh                   # queue a refresh of every feed now
.venv/bin/noadcast reprocess 42 --model qwen/qwen3.8-flash   # old classifications are kept
curl -s http://127.0.0.1:44007/health
```

A restart is safe at any point: interrupted jobs are requeued at boot, and a
partial download resumes from its `.part` file.

## Back up

`data/noadcast.db*` (the database and its WAL files — copy while stopped, or
use `sqlite3 data/noadcast.db ".backup backup.db"` while running) and
`data/llm/` (raw classifier responses). Audio under `data/audio/` is
regenerable from the feeds, and transcripts live in the database.
