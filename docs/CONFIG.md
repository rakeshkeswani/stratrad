# CONFIG.md — environments, paths, shares, settings

Present tense: how things are set up NOW. Update in the same commit as any change.
Last updated: 2026-10-15 (ST-016 closed: backup verified on the stratrad mirror, old mirror removed; older dated rows keep the old name)

## Machines

| | Windows dev | Ubuntu prod |
|---|---|---|
| Machine | Rakesh's laptop | `rkneo50q` — 192.168.0.250 (LAN), 100.103.189.15 (Tailscale) |
| Repo | `C:\RKOneDrive\OneDrive\Work\RKInvesting` | `/home/rakeshbk/stratrad` |
| Python | 3.14 (user site-packages) | 3.12.3 in venv `~/stratrad/venv` (needs `python3.12-venv`) |
| Run things | `python -m <module>` from repo root | `cd ~/stratrad && source venv/bin/activate && python -m <module>` |
| GitHub | `git@github.com:rakeshkeswani/stratrad.git` (private), branch `main` | same, `git pull` only |

`(venv)` in the prod prompt = the venv is active in that shell; `deactivate` leaves it. MyInvestIQ uses
`/usr/bin/python3`, not this venv — run MyInvestIQ scripts with the venv deactivated.

## Folder layout on prod

```
/home/rakeshbk/stratrad/          repo (code)
├── data/                                git-ignored — ALL stratrad data
│   ├── bhavcopy_legacy/                 NSE legacy CM bhavcopy 2014-01-01..2024-07-05, <YYYY>/cm…bhav.csv.zip + manifest.csv
│   ├── bhavcopy_udiff/                  NSE+BSE UDiFF 2024-01-03..2025-08-27 + manifest.csv, plus NSE 2025-02-01 (one-time, read-only)
│   ├── corporate_actions/               ST-003: raw/<YYYY>.json (feed 2014..2025, byte-for-byte) + history.csv
│   ├── symbol_changes/                  ST-003: symbolchange.csv (NSE snapshot) + symbolchange_<date>.csv (previous)
│   └── trendlyne/                       (ST-005)
└── logs/                                git-ignored — fetch/validate logs and issue CSVs
```

**Rule (Rakesh, 2026-10-08):** each project's folder belongs to that project. stratrad never writes under
`/home/rakeshbk/myinvestiq/`; it only reads MyInvestIQ's shared data. MyInvestIQ may read stratrad's data
read-only under its own TD.

## Read-only dependencies on MyInvestIQ

| What | Where | Used for |
|---|---|---|
| UDiFF bhavcopy (NSE+BSE), daily 18:00 job | `/home/rakeshbk/myinvestiq/data/bhavcopy/` | prices from 2025-08-28 (+5 probe dates in 2024) |
| Index closes | `/home/rakeshbk/myinvestiq/data/indices/` | breadth, benchmarks (later) |
| `corporate_actions` table | DB `portfolio_watch` | ongoing splits/bonuses (ST-003; needs read-only role, ST-012) |
| `fii_daily_flow` table | DB `portfolio_watch` | regime model (later) |

## Windows shares

| Drive | UNC | Prod path | Samba section |
|---|---|---|---|
| Z: | `\\100.103.189.15\myinvestiq` | `/home/rakeshbk/myinvestiq` | `[myinvestiq]` |
| X: | `\\100.103.189.15\stratrad` | `/home/rakeshbk/stratrad` | `[stratrad]` (added 2026-10-08) |
| Y: | rkfspool | — | `[rkfspool]` (not this project) |

`[stratrad]` in `/etc/samba/smb.conf`: `path = /home/rakeshbk/stratrad`, `valid users = rakeshbk`,
`create mask = 0644` — identical settings to `[myinvestiq]`. Mapped with
`net use X: \\100.103.189.15\stratrad /persistent:yes`.
Fetchers refuse to write to a `//host/...` path, so Windows can never write into prod's archives.

## Environment variables (`.env`, never committed; template `.env.example`)

| Var | Default (unset) | Prod value | Read by |
|---|---|---|---|
| `BHAVCOPY_ARCHIVE_DIR` | none — required | `/home/rakeshbk/myinvestiq/data/bhavcopy` | `core/udiff_bhavcopy_reader.py` |
| `UDIFF_GAP_DIR` | none | `/home/rakeshbk/stratrad/data/bhavcopy_udiff` | `core/udiff_bhavcopy_reader.py` |
| `LEGACY_BHAVCOPY_DIR` | `<repo>/data/bhavcopy_legacy` | default (may be set explicitly) | legacy reader + fetcher |
| `INDEX_ARCHIVE_DIR` | none | (not yet used) | — |
| `LOG_DIR` | `<repo>/logs` | default | fetch + validate scripts |
| `CORPORATE_ACTIONS_DIR` | `<repo>/data/corporate_actions` | default | CA fetcher, `core/corporate_actions.py`, validator |
| `SYMBOL_CHANGES_DIR` | `<repo>/data/symbol_changes` | default | `fetch_symbol_changes`, `core/symbol_changes.py` |
| `PANEL_DIR` | `<repo>/data/panel` | default | `build_price_panel`, `core/panel.py` |
| `UNIVERSE_DIR` | `<repo>/data/universe` | default | `build_universe`, `core/universe.py` |
| `BENCHMARK_DIR` | `<repo>/data/benchmark` | default | `fetch_benchmark` (Nifty 500 index, `nifty500.csv`) |
| `BACKTESTS_DIR` | `<repo>/data/backtests` | default | `backtest/run.py` (writes; refuses a `//` share), `report_card`, `validate_backtest` |
| `STRATRAD_DB_URL` | — (required for DB scripts) | `postgresql://stratrad_app:<pw>@localhost:5432/stratrad` | `core/db.py` `connect('app')`, `migrate`, `check_database` |
| `STRATRAD_READER_URL` | — | `postgresql://stratrad_reader:<pw>@localhost:5432/stratrad` | `connect('reader')`, `check_database` |
| `MYINVESTIQ_RO_URL` | — | `postgresql://stratrad_ro:<pw>@localhost:5432/portfolio_watch` | `connect('myinvestiq')` (ST-020), `check_database` |
| `KITE_API_KEY`, `KITE_API_SECRET` | — (prod only) | stratrad's own Kite Connect app (Personal, free) | `core/kite.py`; never printed or logged |
| `KITE_SESSION_FILE` | `~/.stratrad/kite_session.json` | default | the day's session (mode 600, folder 700; outside the repo and the backup mirror; expires 06:00 IST) |
| `KITE_CALLBACK_PORT` | `8765` | default | `execution/kite_callback.py` (127.0.0.1 only) |
| `KITE_ORDER_PROXY` | — (order calls fail Kite's IP check without it) | `http://100.70.129.17:8888` | order calls only (ST-007; `scripts/kite_ip_check.py`, later ST-008/009) |
| `KITE_ACCESS_TEAM`, `KITE_ACCESS_AUD` | — (callback refuses everything without them) | `soft-cake-51bb.cloudflareaccess.com`; the AUD tag of Access app "stratrad Kite callback" (not secrets) | `kite_callback` verifies each Access JWT against them |
| `TRENDLYNE_DROP_DIR`, `TELEGRAM_*`, `ANTHROPIC_API_KEY` | — | not yet set | later STs |

Windows dev reading prod data: set `BHAVCOPY_ARCHIVE_DIR=//100.103.189.15/myinvestiq/data/bhavcopy`,
`LEGACY_BHAVCOPY_DIR=//100.103.189.15/stratrad/data/bhavcopy_legacy`,
`UDIFF_GAP_DIR=//100.103.189.15/stratrad/data/bhavcopy_udiff` (forward slashes — dotenv does not unescape `\`).

## Curated config in git (`config/`)

`ca_overrides.csv`, `symbol_changes_extra.csv`, `ca_exceptions.csv` — see docs/CODE_MAP.md. `.gitignore` ignores
`*.csv` except `config/*.csv` (2026-10-11: the first commit silently left these out).

## Database

PostgreSQL 16 on rkneo50q (localhost:5432, server timezone IST), shared with MyInvestIQ; separate databases.

| | |
|---|---|
| Database | `stratrad`, owner `stratrad_app` (ST-012); schema only through `database/migrations/NNN_*.sql` + `python -m scripts.migrate` |
| Roles | `stratrad_app` (owner, live system, migrations); `stratrad_reader` (SELECT only); `stratrad_ro` on `portfolio_watch` (SELECT on `corporate_actions`, `fii_daily_flow` only). All sessions UTC. Created by `database/setup_roles.sql` (superuser, once); passwords set with `\password`, kept only in prod `.env` |
| Checks | `python -m scripts.check_database` (every permission tested by attempting it) |
| Dev (laptop) | same layout inside MyInvestIQ's dev Docker container `portfolio-watch-db` (postgres:16, localhost:5432, superuser `portfolio_user`; timezone UTC): database `stratrad` + the same three roles, created with `database/setup_roles.sql` via `docker exec -i portfolio-watch-db psql -U portfolio_user -d postgres`; dev-only passwords in the laptop `.env`. Migrations run on dev first, then prod. Holds operational test data only; research data stays the single prod copy, read over the share |
| MyInvestIQ | `portfolio_watch`, user `portfolio_rk`; always `psql -h localhost` (peer auth fails on the Unix socket) |

## Backup (owned by the homelab project)

| | |
|---|---|
| Mirror | `/mnt/rkfspool/daily/rkneo50q/stratrad` — FFS Job 3 (`/home/rakeshbk/ffs_jobs/job3_stratrad.ffs_batch`; logs `/home/rakeshbk/logs/ffs_jobs/job3_stratrad <timestamp>.html`). Excludes `*/.Trash-*/`, `*/.recycle/`, `/venv/`, `*/__pycache__/`, `*/.pytest_cache/`, `/.env`, `*/.env` (`.env` since 2026-10-11) |
| Schedule | nightly 23:30, n8n "MyInvestIQ (Daily 23:30) FFS Job 2 BackUp": pgdump portfolio_watch → FFS Job 2 → pgdump stratrad DBase → FFS Job 3 → `Dumps OK?` (exit codes of both dumps; false → Stop and Error, run turns red). n8n's SSH node never fails on a non-zero exit, hence the If node (homelab, 2026-10-16) |
| Database dump | `stratrad` (ST-012, since 2026-10-16): `/home/rakeshbk/homelab/stratrad-dump/stratrad_dump.sh` → `pg_dump -Fc` to `data/db_backups/stratrad_YYYYMMDD.dump.part`, checked non-empty and readable by `pg_restore --list`, renamed; newest 14 kept; on failure the `.part` is deleted and the script exits non-zero. Auth `~/.pgpass` (stratrad_app; no password in n8n). pg_dump/pg_restore 16.15 = server. Reaches the mirror and Kopia via Job 3 (`data/db_backups` not excluded) |
| Kopia | daily 01:00 snapshot of `/mnt/rkfspool/daily` (repo `/mnt/rkfspool/backup`; retention 30 daily / 52 weekly / 24 monthly / 2 annual). First snapshot with strategy-trader: 2026-10-11 01:00; first with `rkneo50q/stratrad`: `k13eb1c5` 2026-10-15 01:00 |
| Freshness alert | `/usr/local/bin/homelab-health.sh` daily (02:30): check 4 newest `job3_stratrad *.html` log <= 48 h old and "Completed successfully" (since 2026-10-11; renamed 2026-10-14); check 5 newest `stratrad_*.dump` <= 26 h old and >= 1 KB (since 2026-10-16; 26 h like portfolio_watch, so one missed night alerts) |
| Verified | 2026-10-14 by the homelab project after the ST-016 rename: manual Job 3 run completed (4,007 items, 569 MB); mirror complete (3,792 files, 568,613,234 bytes both sides, 0 differences); exclusions hold; n8n Job 3 node runs `job3_stratrad.ffs_batch`. 2026-10-15: first scheduled Job 3 run 2026-10-14 23:31 completed; Kopia `k13eb1c5` (01:00) contains `rkneo50q/stratrad` with all data folders incl. `panel`, `universe`; restore of `data/corporate_actions/history.csv` SHA-256 `5430459e…9bf26670` = live file (Claude re-checked via X:); 02:30 health run ok, check 4 PASS. Old mirror `rkneo50q/strategy-trader` (551 MB) removed 2026-10-15 10:52 IST (still in earlier snapshots). Checks 1-5 and 7 all PASS on the new path |

FFS rule (homelab, 2026-10-14): batch files cancel on any warning, so a new FFS target folder must exist before the
job's first run (the first manual stratrad run stopped until the folder was created).

`.env` must never reach the mirror once it holds Kite keys (ST-006). The mirror is plain text on the `Y:` share.
`.env` is also hidden on the stratrad Samba share (`veto files = /.env/` on `[stratrad]`, 2026-10-16; `X:\.env` is
refused): check prod `.env` from a prod shell, masked.

Database dump verified 2026-10-16 (by hand): `stratrad_20261016.dump` 55,393 bytes at 12:57:39 IST, `pg_restore
--list` = 15 tables incl. `audit_log`, `schema_migrations`; failure test (missing database) exit 1, no `.part` left.
Scheduled runs verified 2026-10-24 (homelab): 8 nightly dumps 16-23 Oct at 23:31, identical in the pool mirror,
`pg_restore --list` of 23 Oct lists `audit_log` and `schema_migrations`, check 5 OK. Restores: from
`~/stratrad/data/db_backups/` (laptop: `X:\data\db_backups\`, read-only) or Kopia, always into a scratch database
(`createdb stratrad_restore_test && pg_restore -d stratrad_restore_test <dump>`), never over prod. Tell the homelab
project if the database name, role or `~/.pgpass` entry changes (the dump script depends on them).

## Kite login (ST-006)

| | |
|---|---|
| App | stratrad's own Kite Connect **Personal** app (free; orders, GTT, portfolio; no market data). The older Personal app "Zerodha Integration" (2025-08-18) is left untouched |
| Daily login | manual: Rakesh opens the bookmarked login link (`https://kite.zerodha.com/connect/login?v=3&api_key=<KITE_API_KEY>`), password + TOTP. Not automated (regulation's intent; his daily "trade today"). No login = no trading that day |
| Redirect URL | `https://kite.myinvestiq.ai/kite/callback` -> Cloudflare tunnel -> `127.0.0.1:8765` on rkneo50q; tunnel rule host `kite.myinvestiq.ai`, path `^/kite/callback$` only (other paths 404 at the tunnel); Cloudflare Access app "stratrad Kite callback" (policy "Allow rakeshbk only": `rakeshbk@gmail.com`, one-time PIN, 1-month session); the callback verifies the `Cf-Access-Jwt-Assertion` JWT (RS256 against the team's keys, audience = AUD tag, issuer = team, expiry) and fails closed (503 when the keys are unreachable or the settings missing). Homelab: tunnel config `/etc/cloudflared/config.yml` (2026-10-16) |
| Service | systemd `stratrad-kite-callback` (`deploy/systemd/stratrad-kite-callback.service`, user rakeshbk, restart on failure); log `LOG_DIR/kite_callback.log` (never the query string or a token) |
| Session | `KITE_SESSION_FILE`; refused by `core/kite.py` once issued before the most recent 06:00 IST; each login -> `audit_log` (`kite_login`, masked user, no token) |
| Check | `python -m scripts.kite_check` (read-only: masked user, equity funds, holdings count) |
| Static IP | only order endpoints need it (SEBI, since 2026-04-01). ST-007: **`13.205.57.58`** (AWS Lightsail Mumbai `stratrad-kite-exit`, static IP `stratrad-kite-exit-ip`), registered as the primary IP in the Kite developer profile 2026-10-27. Order calls go through `KITE_ORDER_PROXY` (tinyproxy on the VM's Tailscale address `100.70.129.17:8888`, accepting only rkneo50q `100.103.189.15`, connecting only to `api.kite.trade`); login/profile/holdings/margins stay direct. VM is `tag:kite-exit` on the tailnet: destination only (homelab grants + policy tests). Check: `python -m scripts.kite_ip_check` |

## Scheduled jobs

None yet. (n8n at http://192.168.0.250:5678 runs MyInvestIQ only.)

## One-off operations already run (do not repeat)

| Date | What | Script / command |
|---|---|---|
| 2026-10-08 | Legacy bhavcopy 2014-01-01..2024-07-05 + 7 weekend sessions | `python -m ingestion.fetch_legacy_bhavcopy` |
| 2026-10-08 | UDiFF 2024-01-01..2025-08-27 fetched with MyInvestIQ's fetcher, then moved out of its archive and its manifest rows removed (backup `manifest.csv.bak_<time>` in MyInvestIQ's archive) | `scripts/relocate_udiff_gap.py --go` (paths in it are now historical) |
| 2026-10-08 | Both data folders moved from `myinvestiq/data/` to `strategy-trader/data/`; X: share added | manual `mv`, smb.conf edit |
| 2026-10-14 | Universe: 25 Nifty 500-aligned reviews (cut-offs 2014-07-31..2026-07-31), 12,500 rows, `data/universe/membership.csv` 1.1 MB, 11 s; validator PASS | `python -m ingestion.build_universe` (re-run after each new cut-off or a panel rebuild) |
| 2026-10-14 | Price panel 2014-01-01..2026-10-13: 5,399,886 rows, 3,156 trading days, 3,127 companies, 3,136 series, 165.4 MB, built in 59 s | `python -m ingestion.build_price_panel --full` (re-run after a reader fix or a new rename; plain run appends new days) |
| 2026-10-11 | NSE symbol-change snapshot (1,056 renames, newest 2026-09-25) | `python -m ingestion.fetch_symbol_changes` (re-run only to refresh; keeps the previous snapshot) |
| 2026-10-11 | Corporate-actions history 2014..2025-08-25: raw/2014..2025.json + history.csv (805 rows) | `python -m ingestion.fetch_corporate_actions_history` (re-run reuses raw/; `--force` re-fetches) |
| 2026-10-11 | NSE UDiFF for Saturday 2025-02-01 (Budget session) into `data/bhavcopy_udiff/` — 167,634 bytes, 2,262 EQ/BE symbols; no manifest row | `curl` with browser User-Agent + Referer from `nsearchives.nseindia.com/content/cm/` |
