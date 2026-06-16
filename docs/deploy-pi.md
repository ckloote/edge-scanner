# Deploying on a Raspberry Pi

Run the scanner continuously on a Pi and view the dashboard from another machine on
your network. Two services share one SQLite file (WAL mode): the **scanner** writes,
the **dashboard** reads — no contention.

Prereqs: **64-bit Pi OS** recommended (prebuilt arm64 wheels for pandas/streamlit;
32-bit armv7 may compile from source, slowly). The Pi needs network access to the
venue APIs. Commands below assume user `ckloote` and a checkout at `~/edge-scanner`
— adjust to taste.

## 1. Install uv (and put it on systemd's PATH)

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
# systemd services run with a minimal PATH; symlink uv where they can find it:
sudo ln -sf "$HOME/.local/bin/uv" /usr/local/bin/uv
```

## 2. Clone and sync

```bash
cd ~
git clone git@github.com:ckloote/edge-scanner.git    # or https:// with a PAT
cd edge-scanner
uv sync --extra dashboard --no-dev                   # installs runtime + streamlit/pandas
uv run python -c "import scanner; print('ok')"
```

uv reads `.python-version` (3.11) and provisions that interpreter. `uv.lock` pins
exact versions, so the env matches your dev machine.

## 3. (Optional) review config

- `config/settings.toml` → set `risk_free_rate` to the current short T-bill yield
  (placeholder is 0.043). For a multi-week run on an SD card you may want to raise
  `poll_interval_seconds` (e.g. 30) to slow DB growth — edges move slowly, so window
  durations are still well captured (see Maintenance).
- `config/links.yaml` already holds the 14 curated links.

## 4. Install the two services

```bash
# Point both units at this checkout/user, then install them:
sed -i "s|/home/ckloote/.*edge-scanner|$HOME/edge-scanner|; s|^User=.*|User=$USER|" \
    deploy/scanner.service deploy/dashboard.service
sudo cp deploy/scanner.service   /etc/systemd/system/edge-scanner.service
sudo cp deploy/dashboard.service /etc/systemd/system/edge-scanner-dashboard.service
sudo systemctl daemon-reload
sudo systemctl enable --now edge-scanner.service edge-scanner-dashboard.service
```

Both have `Restart=always` and `WantedBy=multi-user.target`, so they survive crashes
and reboots.

## 5. View the dashboard from your laptop

Find the Pi's address (`hostname -I`, or use the mDNS name), then browse to:

```
http://<pi-ip>:8501          # e.g. http://192.168.1.50:8501
http://raspberrypi.local:8501 # if your laptop resolves mDNS / .local
```

Pi OS ships no firewall by default. If you enabled `ufw`: `sudo ufw allow 8501/tcp`.

> **Security:** this exposes the dashboard (no auth) on your LAN — fine for a trusted
> home network. To avoid exposing it, set `--server.address 127.0.0.1` in
> `dashboard.service` and use an SSH tunnel from your laptop instead:
> `ssh -L 8501:localhost:8501 ckloote@<pi-ip>` then open `http://localhost:8501`.

## Checking on it

```bash
systemctl status edge-scanner edge-scanner-dashboard   # are they up?
journalctl -u edge-scanner -f                           # live scanner logs
uv run python scripts/report.py                         # one-shot results summary (read-only)
```

`scripts/report.py` prints row counts, the edge-history time span, the latest edge per
event (sorted by net, positive ones starred), and any Manifold paper trades — handy
over SSH without opening the dashboard.

## Maintenance

- **DB growth.** At a 10 s interval the 14 links write ~56 quote rows/cycle (~0.5 M/day).
  Over weeks that's substantial on an SD card — watch `du -h data/edge_scanner.db`, and
  raise `poll_interval_seconds` if needed. (A retention/rotation job for the `quote`
  table is a known follow-up.)
- **Updating.** `git pull && uv sync --extra dashboard --no-dev && sudo systemctl restart
  edge-scanner edge-scanner-dashboard`.
- **Clock.** Ensure NTP is on (Pi OS default) — timestamps drive lockup/horizon math.

## Phase 4: the study

Let it run for several weeks, then analyse `edge_snapshot`: how often a genuine,
after-fee, **executable**, near-dated edge appears and how long each window stays open
— always broken out by `basis_risk_flag`. The dashboard's net-edge-over-time chart and
`scripts/report.py` are the starting points.
