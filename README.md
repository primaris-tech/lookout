# Lookout

Self-hosted forward-looking severe weather alerts driven by the NWS Storm Prediction Center.

## What it does

Watches SPC convective outlooks and mesoscale discussions for your configured locations and notifies you on five events:

| Event | Fires when |
|---|---|
| `first_appearance` | A risk first appears for a target date (e.g., Wednesday now appears in the Day 4 outlook) |
| `risk_upgrade` | Severity increases for a target date (MRGL → SLGT, SLGT → ENH, …) |
| `day_shift_closer` | Same risk now appears in a smaller-N outlook (Day 4 → Day 3 — confidence is firming) |
| `risk_cleared` | A previously-watched risk has been removed (gated by peak risk so cleared events for risks below your threshold are suppressed) |
| `mesoscale_discussion` | SPC issues a Mesoscale Discussion whose polygon covers a configured location |

Notifications go through [Apprise](https://github.com/caronc/apprise) — email, ntfy, Pushover, Discord, Slack, Telegram, Gotify, Matrix, and ~95 other services are supported via one config string.

## What it deliberately doesn't do

Lookout does **not** issue watches, warnings, or any product NWS already pushes. NWS Wireless Emergency Alerts, NOAA Weather Radio, and your standard weather apps are the authoritative source for those. Lookout fills the gap *before* those products: knowing on Sunday that Wednesday could be active, or seeing that SPC has begun watching a developing storm complex covering your area.

## Supported products

| Product | Coverage | Issuance |
|---|---|---|
| Day 1 categorical | TSTM, MRGL, SLGT, ENH, MDT, HIGH | 5×/day |
| Day 2 categorical | same | 2×/day |
| Day 3 categorical | same | 1×/day |
| Day 4–8 probabilistic | 15%/30%/45%/60% mapped to MRGL/SLGT/ENH/MDT | 1×/day |
| Mesoscale Discussions | per-MD polygon | ad hoc |

Per-hazard probabilistic outlooks (tornado/wind/hail) are acknowledged in the config schema but not yet implemented.

## Quick start (Docker)

```
git clone <repo-url> lookout
cd lookout
cp config.example.yml config.yml
$EDITOR config.yml
docker compose up -d
docker compose logs -f
```

State persists in `./data/`. Editing `config.yml` doesn't require a rebuild — restart the container to pick up changes.

## Quick start (without Docker)

Requires Python 3.11+.

```
python -m venv .venv
source .venv/bin/activate
pip install -e .
cp config.example.yml config.yml
$EDITOR config.yml
lookout
```

## Kubernetes

Manifests in `k8s/` deploy Lookout against the published image at `ghcr.io/primaris-tech/lookout:latest` (built by `.github/workflows/docker-latest.yml` on every push to `main`; release tags `v*` produce `:<version>`).

```
cp k8s/config-secret.example.yaml k8s/config-secret.yaml
$EDITOR k8s/config-secret.yaml            # paste your config.yml under stringData
kubectl apply -f k8s/config-secret.yaml
kubectl apply -f k8s/lookout.yaml
```

`k8s/config-secret.yaml` is gitignored — keep it local or store it in a secrets manager (Vault, SOPS, etc.).

Pin to a release tag in production:

```
kubectl set image deployment/lookout lookout=ghcr.io/primaris-tech/lookout:<version> -n lookout
```

When pinning, also set `imagePullPolicy: IfNotPresent` in `k8s/lookout.yaml` — `Always` is the default there because `:latest` doesn't otherwise re-pull on pod restart.

Notes:

- **Single replica only.** SQLite is the state store; the Deployment uses `strategy: Recreate` and a `ReadWriteOnce` PVC. Don't scale up.
- **Namespace `lookout`** is created by the manifest.
- **State** lives on the `lookout-data` PVC (1Gi by default; resize if you add many locations or shorten the polling interval).
- **Pod security**: runs as UID 1000 non-root with a read-only root filesystem; `/tmp` is an `emptyDir` for scratch space.

## Configuration

Everything is in `config.yml`. See `config.example.yml` for a fully-annotated reference. The minimum:

```yaml
user_agent_contact: "you@example.com"   # required: NWS guidelines ask for contact info in User-Agent
alert_threshold: MRGL                    # default; per-location overrides allowed

locations:
  home:
    lat: 35.7796
    lon: -78.6382
    # threshold: SLGT     # optional override

notification_channels:
  my_phone: "ntfys://my-private-topic"

notification_rules:
  - name: "Me"
    locations: [home]
    channels: [my_phone]
```

### Locations
Map of `name → {lat, lon, threshold?}`. Per-location threshold falls back to the global `alert_threshold` when not set.

### Risk thresholds
Six categorical levels in ascending severity: `TSTM`, `MRGL`, `SLGT`, `ENH`, `MDT`, `HIGH`. Events fire only when severity ≥ the effective threshold.

### Notification channels
A named map of `channel_name → Apprise URL`. The [Apprise wiki](https://github.com/caronc/apprise/wiki) lists URL formats per service.

### Notification rules
Each rule routes matching events to one or more channels. A rule fires when **all** its filters match the event:

- `locations` — event's location must be in this list
- `products` (optional) — event's product type must be in this list (`convective_outlook`, `mesoscale_discussion`)
- `min_threshold` (optional) — event severity must be ≥ this threshold

A rule's `min_threshold` can only **raise** the effective threshold above the location's; it cannot lower it. The location threshold is the user's floor for that place; rules can narrow further upward for specific audiences (e.g., only ENH+ goes to the family group chat) but never below the location floor.

Multiple rules can match a single event — each fires independently to its own channels. Channels are deduplicated within a single event.

## Running

```
lookout                            # polling loop (default; use this in production)
lookout --fetch-once               # run one cycle and exit
lookout --check                    # validate config and exit
lookout --dry-run --fetch-once     # preview what would happen, no dispatch, no DB writes
```

`--dry-run` skips both notification dispatch and database commits — you can preview the same cycle as many times as you want without "consuming" the first-run seed.

Useful flags:

- `--config PATH` (default: `./config.yml`)
- `--db PATH` (default: `./data/lookout.db`)
- `--log-level {DEBUG,INFO,WARNING,ERROR}` (default: `INFO`)

## Operational notes

- **Polling cadence** defaults to 10 minutes (`polling.interval_minutes`). Lookout uses hash-based dedup so unchanged outlooks are skipped without re-parsing.
- **First-run silent seed:** when a location has no prior state, Lookout populates state from current outlooks without firing per-event alerts, then sends one summary message ("Currently watching home: SLGT Wed, MRGL Thu"). Subsequent cycles use diff-based alerts. New locations added to config get the same treatment on first observation.
- **Fetch-failure meta-alert:** if Lookout can't reach SPC for `polling.meta_alert_after_minutes` (default 60), it broadcasts a meta-alert through every channel referenced by any rule. A recovery alert fires when SPC is reachable again — but only if a meta-alert was previously sent, so brief outages stay quiet.
- **State retention:** rows older than `state_retention_days` (default 30) are pruned each cycle. SQLite handles this in milliseconds.
- **Restart safety:** in-memory hash dedup is cleared on restart, so the next cycle re-fetches and re-parses. The diff engine compares observations to persisted state, so no duplicate alerts fire — restarts are idempotent.
- **Graceful shutdown:** SIGINT/SIGTERM break the polling loop cleanly. Docker `compose down` and Ctrl-C both produce a clean exit.

## Example notification

```
Lookout — SLGT risk for home (Wed May 7)

home is in SLGT risk for Wed May 7.
Currently in the Day 3 outlook.
More: https://www.spc.noaa.gov/products/outlook/day3otlk.html
```

## State and persistence

SQLite at `./data/lookout.db` (configurable). Tables:

- `outlook_state` — current observed risk per `(location, target_date)`. The diff engine compares observations to this.
- `mesoscale_discussion_alert` — idempotency log; one row per `(mcd_id, location)` we've already alerted for.
- `location_seed` — seed markers; presence means the location has completed its first-run population.
- `fetch_failure_state` — singleton tracking the active failure window for meta-alerts.

You can inspect state directly with `sqlite3 ./data/lookout.db`.

## Limitations / known gaps

- **D4–8 probabilistic schema is best-effort.** SPC's actual probability `LABEL`/`DN` encoding wasn't observable when the parser was written (D4–8 frequently shows "Predictability Too Low" with no real probability features). The 15%→MRGL/30%→SLGT/45%→ENH/60%→MDT mapping follows SPC's public conventions but should be verified against real data once severe weather returns.
- **MD polygon parsing depends on SPC's text-product format.** Mesoscale Discussions aren't published as GeoJSON; coordinates live inside the text product as a `LAT...LON` block of 8-digit codes. SPC has been consistent with this format for years, but it's an undocumented dependency. If SPC changes the format, the MD parser will need updating.
- **Per-hazard probabilistic outlooks** (tornado/wind/hail individually) aren't implemented — only aggregate "any severe" via the categorical (D1–3) and probabilistic (D4–8) products.
- **Time zones in messages** are currently UTC-derived (target_date is the SPC convective day, 12Z → 12Z). No localization yet.
