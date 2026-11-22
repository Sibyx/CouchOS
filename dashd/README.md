# dashd

The CouchOS dashboard daemon. Gathers widget data (clock, weather, the frog of the day) and serves it to every client: the TV, the phone app, the web page, anything on the LAN. No auth, like couchd.

```
 Open-Meteo ─┐                 ┌─ GET /v1/views/tv ─► screensaver.couchos.dashboard (Kodi draws it)
 iNaturalist ┼─► dashd :8766 ──┼─ GET /v1/dashboard ─► app, web page, scripts
 (station) ──┘   (Rust)        └─ JSONRPC.NotifyAll "dashd/changed" ─► Kodi (no polling)
```

**On the TV** the dashboard is Kodi's screensaver: Kodi shows it when the TV is idle (5 min), never over a video, and the first key from the TV remote or the phone goes back to Plex. couchd's `POST /api/dashboard {"show": true}` wakes the TV over CEC and opens it now (the app has it under *Open on TV → Dashboard*). `{"enabled": false}` turns the idle screen back to black.

## Design

| Module | Job |
|---|---|
| `widget.rs` | **The extension point.** `Widget` trait: `initial` (fixed or saved output), `next_refresh` + `refresh` (polled), `handle` (client input). `Output` = `data` (structured JSON) + `view` (flat display strings for Kodi) |
| `widgets/` | The kinds. A new kind = a module + one line in `KINDS` |
| `hub.rs` | Widgets + store + notifiers behind `refresh` / `handle`, used by the scheduler and the API alike |
| `store.rs` | Latest output per widget; response bodies serialized once per change, shared as `Arc<[u8]>`, version = ETag |
| `scheduler.rs` | One thread, sleeps until the earliest due widget or an API "refresh now"; backoff 30 s → 15 min on failure, old data kept and marked `stale` |
| `notify.rs` | `Notifier` trait; `KodiNotifier` (JSON-RPC `NotifyAll`). More listeners (SSE, MQTT) plug in here |
| `net.rs`, `clock.rs`, `assets.rs`, `state.rs` | Ports to the outside world (HTTP out, local time, files served, JSON kept across restarts), each a small type or trait so widgets are testable with `testing.rs` fakes |
| `http.rs` | The API (below). Routing is a plain function, tested without sockets |
| `config.rs` | `dashd.json` (Ansible renders it from `dashd_widgets`); no file = the default Bratislava dashboard |

Lean by design: a 1.8 MB binary for the Pi; 9.7 MB resident measured on macOS, which counts shared system libraries (on the Pi, check `systemctl --user status dashd`). It uses 4 crates (serde, serde_json, tiny_http, ureq/rustls). Idle, it uses no CPU: no ticking, the scheduler sleeps until the next refresh, and the TV re-reads only when notified. Unchanged polls get a bodiless 304. Photos are streamed from disk, not held in RAM. Release builds use LTO, `panic=abort` and are stripped. The unit sets `MALLOC_ARENA_MAX=1` and `MemoryMax=32M`.

## API

```
GET  /v1/health
GET  /v1/dashboard             {version, widgets: [{id, kind, updated_at, stale, error, data}]}
GET  /v1/views/tv              {version, properties: {"weather.temp": "2°", "frog.name": "...", ...}}
GET  /v1/widgets/{id}          one envelope
POST /v1/widgets/{id}          JSON input for pushed/stateful widgets (405 for the others)
POST /v1/widgets/{id}/refresh  poll it now
GET  /v1/assets/{name}         files widgets serve (frog-<date>.jpg; a new name each day)
```

All JSON answers carry `ETag`. Send `If-None-Match` to get a 304. Through Caddy the same paths are on port 80 (`http://beelzebub-tv.local/v1/dashboard`).

## Widgets

- **clock**: the TV draws it from Kodi's own time (`$INFO[System.Time(HH:mm)]`), so it costs nothing. The widget places it on the dashboard and names the timezone.
- **weather**: Open-Meteo, no API key, every 15 min: now, the next 12 h in 2-hour steps, and 5 days. The TV view maps WMO codes to Kodi's stock icon set (`resource.images.weathericons.default`, day and night variants) and shows precipitation from 20 %. Params: `place`, `latitude`, `longitude`, `timezone`, `every_minutes`.
- **frog**: one of ~7,900 Anura species on iNaturalist each day. The pick is a hash of the local date, so it is the same frog after a restart. It shows the Latin name, common name, family, IUCN status, a Wikipedia summary and a CC-licensed photo with its attribution; photos without a licence are skipped. The TV gets a shorter summary: whole sentences up to about 170 characters, without the opening "… is a species of frog in the family …". `data.summary` keeps the full text. The pick and its photo are saved, so the day costs three API calls. Param: `taxon_id` (default 20979, Anura).

## Adding a widget

1. `src/widgets/<kind>.rs`: a `build(&Value) -> Result<Box<dyn Widget>>` and an `impl Widget`. Implement only what applies:
   - **Polled** (a web API): `next_refresh` + `refresh`.
   - **Pushed** (the weather station POSTs readings): `handle`. Return the new `Output` and dashd stores it, bumps the version and notifies Kodi. Keep the last reading with `ctx.state` so a restart shows it.
   - **Stateful with commands** (TODO from the app): `handle` takes `{"op": "add", "text": ...}` / `{"op": "done", "id": ...}`, updates its list in `ctx.state`, and returns the whole list. `initial` loads it at start.
2. Add it to `KINDS` in `src/widgets/mod.rs` and to `dashd_widgets` in `ansible/group_vars/all.yml`.
3. For the TV: put its `view` keys in `screensaver-couchos-dashboard.xml` as `$INFO[Window.Property(<id>.<key>)]`. The phone and web read `data` instead.

```sh
cargo test                # unit tests, macOS or Linux
cargo run                 # serves the default dashboard on :8766 (DASHD_CONFIG=... for another)
./build.sh                # → dist/dashd-<version>-trixie-arm64 (Docker, arm64, Pi's glibc)
```

## Ideas for later

- **Home**: the weather station (indoor/outdoor temperature, humidity, CO₂, pressure trend) beside the forecast; "open a window" when CO₂ is high.
- **TODO / shopping list**: edited from the app, done items fade out on the TV.
- **Getting around**: next trams and buses from the nearest stop (IDS BK / DPB departures), with "leave in 4 min".
- **Calendar**: today's events from a shared iCal/CalDAV feed; birthdays and name days (meniny: the Slovak name-day calendar).
- **Air and sky**: air quality (Open-Meteo's air-quality API: PM2.5, pollen in spring), UV index, sunrise and sunset as a day arc, moon phase.
- **From CouchOS itself**: "Continue watching" from the Plex index with a poster, newly added movies, the NAS disk filling up, a Pi under-voltage warning.
- **Photo of the day** from `/srv/media/Photos` ("on this day, years ago"), alternating with the frog.
- **Evening mode**: dim and warm after sunset, and CEC standby at night when nothing has played for an hour.
- **Plumbing**: SSE `/v1/events` for the app (a `Notifier`), widget schedules (e.g. trams only 6–9 am), several dashboard pages rotating.
