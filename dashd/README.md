# dashd

The CouchOS dashboard daemon. Gathers widget data (clock, today's weather with a duck dressed for it, a message and TODO list written from the app, the frog of the day, the app's QR code) and serves it to every client: the TV, the phone app, the web page, anything on the LAN. No auth, like couchd.

On the TV, a fixed grid on black, so nothing overlaps: three columns (time, date and today's weather; the duck and its line in a yellow bubble; the frog of the day) over a row of three equal card slots. The three columns are always there, the duck and the frog's photo included. The row is flexible: it shows only cards with something in them, side by side, and a hidden card leaves its slot to the next. The cards, in order:
- a **message** from the app, while there is one: first, yellow, two slots wide, in big type (smaller past 110 characters). The next days step aside for it; the third slot is the TODO list, else what is playing, else the QR code
- the TODO list, while it has items
- **what is playing**, while audio plays (AirPlay from Spotify and the like, Plex music: artwork, title, artist, album, progress; straight from Kodi, not dashd)
- the next days
- the app's QR code

By the time of day (widget `mode`, below), the TV dims after sunset and at night shows only the time, dimmed further.

Whatever doesn't fit shares the last slot as a carousel, one slide every 20 s. With nothing on the board and nothing playing, the next days go wide (five days) beside the QR code. When AirPlay starts playing audio only, Kodi opens the dashboard by itself (`service.couchos`, `watch.py`).

```
 Open-Meteo ─┐                 ┌─ GET /v1/views/tv ─► screensaver.couchos.dashboard (Kodi draws it)
 iNaturalist ┼─► dashd :8766 ──┼─ GET /v1/dashboard ─► app, web page, scripts
 (station) ──┘   (Rust)        └─ JSONRPC.NotifyAll "dashd/changed" ─► Kodi (no polling)
```

**On the TV** the dashboard is Kodi's screensaver: Kodi shows it when the TV is idle (5 min, also while music plays), never over a video, and the first key from the TV remote or the phone goes back to Plex. The window itself lives in the always-running `service.couchos`, which keeps the last view and opens it the moment Kodi announces the screensaver (no Python interpreter to start). couchd's `POST /api/dashboard {"show": true}` wakes the TV over CEC and opens it now (the app has it under *Open on TV → Dashboard*). `{"enabled": false}` turns the idle screen back to black.

## Design

| Module | Job |
|---|---|
| `widget.rs` | **The extension point.** `Widget` trait: `initial` (fixed or saved output), `next_refresh` + `refresh` (polled), `handle` (client input). `Output` = `data` (structured JSON) + `view` (flat display strings for Kodi) |
| `widgets/` | The kinds. A new kind = a module + one line in `KINDS` |
| `hub.rs` | Widgets + store + notifiers behind `refresh` / `handle`, used by the scheduler and the API alike |
| `store.rs` | Latest output per widget; response bodies serialized once per change, shared as `Arc<[u8]>`, version = ETag |
| `scheduler.rs` | One thread, sleeps until the earliest due widget or an API "refresh now"; backoff 30 s → 15 min on failure, old data kept and marked `stale` |
| `notify.rs` | `Notifier` trait; `KodiNotifier` (JSON-RPC `NotifyAll`). More listeners (SSE, MQTT) plug in here |
| `duck.rs` | The duck: config rules turn the weather into an outfit, then SVG blocks (sky, ground, rain/snow, the duck, each garment) are layered into one picture |
| `svg.rs` | SVG → PNG (resvg; shapes only, no fonts). Kodi cannot draw SVG, so pictures are rasterized once per change and served as files named by their content hash |
| `sun.rs` | Sunrise and sunset from latitude/longitude, computed locally (within a few minutes), for rules like `sunset+30` |
| `net.rs`, `clock.rs`, `assets.rs`, `state.rs` | Ports to the outside world (HTTP out, local time, files served, JSON kept across restarts), each a small type or trait so widgets are testable with `testing.rs` fakes |
| `http.rs` | The API (below). Routing is a plain function, tested without sockets |
| `config.rs` | `dashd.json` (Ansible renders it from `dashd_widgets`); no file = the default Bratislava dashboard |

Lean by design: a 2.6 MB binary for the Pi (1.8 MB before the SVG rasterizer); 9.7 MB resident measured on macOS, which counts shared system libraries (on the Pi, check `systemctl --user status dashd`). It uses 6 crates (serde, serde_json, tiny_http, ureq/rustls, resvg without text or raster images, qrcodegen). A duck picture (a 512 px PNG) is rendered only when the outfit or sky changes, and only the current one is kept. Idle, it uses no CPU: no ticking, the scheduler sleeps until the next refresh, and the TV re-reads only when notified. Unchanged polls get a bodiless 304. Photos are streamed from disk, not held in RAM. Release builds use LTO, `panic=abort` and are stripped. The unit sets `MALLOC_ARENA_MAX=1` and `MemoryMax=32M`.

## API

```
GET  /v1/health
GET  /v1/dashboard             {version, widgets: [{id, kind, updated_at, stale, error, data}]}
GET  /v1/views/tv              {version, properties: {"weather.temp": "2°", "frog.name": "...", ...}}
GET  /v1/widgets/{id}          one envelope
POST /v1/widgets/{id}          JSON input for pushed/stateful widgets (405 for the others)
POST /v1/widgets/{id}/refresh  poll it now
GET  /v1/assets/{name}         files widgets serve (frog-<date>.jpg, duck-<hash>.png/.svg, qr-<hash>.png; a new name whenever the picture changes)
```

All JSON answers carry `ETag`. Send `If-None-Match` to get a 304. Through Caddy the same paths are on port 80 (`http://beelzebub-tv.local/v1/dashboard`).

## Widgets

- **clock**: the TV draws it from Kodi's own time (`$INFO[System.Time(HH:mm)]`), so it costs nothing. The widget places it on the dashboard and names the timezone.
- **weather**: Open-Meteo, no API key, every 15 min: now, the next hours, the next days, and the duck. The TV shows today (now, feels like, high/low, rain, sunrise–sunset, "next hours" columns, the duck and its line) and the next days: four, or five on the wide card (`day1`…`day5` keys, always served, blank past the forecast); `data.days` has all `days`. WMO codes map to Kodi's stock icon set (`resource.images.weathericons.default`, day and night variants); precipitation shows from 20 %. Params: `place`, `latitude`, `longitude`, `timezone`, `every_minutes` (15), `hours_step` (3), `hours_count` (4, max 6), `days` (5), `duck` (below).
- **the duck** (`weather.params.duck`): dressed for now and the next `look_ahead_hours` (3): the coldest "feels like" and the worst sky in that window, where an hour's rain or snow counts from `min_chance` % (30). Rules run in order; each one whose `when` holds puts items on slots, a later rule wins a slot, `none` empties it, and the last matching `say` is the duck's line. The defaults are in `default_rules()` in `src/duck.rs`: sun hat, sunglasses and ice cream from 26°; a cap from 18°; a sweater below 18°; a scarf below 10°; a beanie and winter jacket below 3°; mittens and boots below -3°; raincoat, rain hat, umbrella and boots in rain; everything warm in snow; a lantern in fog. The scene follows the sky (sun, moon and stars, clouds, rain, snow on the ground, lightning, fog bands, wind lines and a flying scarf from `windy_kmh`, 30).
  - `extra_rules`: appended to the defaults. `rules`: replace them.
  - A rule: `{"when": {...}, "wear": {slot: item}, "say": "..."}`. `when` (all optional, all must hold): `feels_below`, `feels_at_least`, `sky` (list of `clear`, `partly_cloudy`, `cloudy`, `fog`, `drizzle`, `rain`, `freezing_rain`, `snow`, `storm`), `rain_chance_at_least`, `wind_at_least`, `night`. `say` fills in `{temp}`, `{feels}`, `{high}`, `{low}`, `{rain_chance}`, `{wind}`.
  - Slots and items: `head` (beanie, cap, sun_hat, rain_hat), `face` (sunglasses), `neck` (scarf), `top` (sweater), `coat` (winter_jacket, raincoat), `hands` (mittens), `feet` (boots), `held` (umbrella, ice_cream, lantern). A wrong slot is a config error at start.
  - `colors`: `{"duck": "#FFD23F", "scarf": "#E63946", ...}`; every name is in `PALETTE` in `src/duck.rs`. `enabled: false` removes the duck.
  - `data.duck` has the outfit, the sky, the line, and both pictures (`image` PNG, `svg`). `DUCK_PREVIEW=/dir cargo test duck::tests::preview -- --ignored` renders a gallery of typical days.
- **board**: a message and a TODO list, written from the app (Settings → Dashboard) or anything that can POST. Kept in `$XDG_STATE_HOME/dashd/<name>.json`, so a restart keeps it. Every answer is the whole board:
  ```
  POST /v1/widgets/board  {"op": "message", "text": "Dinner at 7?"}   ("text": "" takes it down; an optional "from" is kept in data, not shown)
                          {"op": "add", "text": "Buy milk"}        {"op": "done", "id": 3}   ("done": false reopens)
                          {"op": "edit", "id": 3, "text": "..."}   {"op": "remove", "id": 3}   {"op": "clear_done"}
  ```
  The TV lists open items first, then done ones dimmed for `keep_done_hours` (12); the app keeps them until cleared. Text is one line per item (80 chars), the message up to 280 chars; on the TV, `[` `]` are shown as `(` `)` (Kodi markup) and emoji are dropped (Kodi's fonts have none). Params: `name` ("board"), `rows` (6), `keep_done_hours`, `message_hours` (0 = until replaced).
- **qr**: a QR code drawn once at start, at a whole number of pixels per module so it stays crisp. The TV's `app` widget points at the app's TestFlight invite (`app_build_url` in Ansible); an empty `url` hides it. Params: `url`, `title`, `subtitle`.
- **daypart** (id `mode`; the TV skin reads it by that id): the time of day. It names the **mode** the dashboard is in now, and the TV applies it. A mode is a bundle of display settings: `layout` (`full`, everything; `clock`, only the time, big, on black), `dim` (0–90 %, a veil over the whole screen) and `tint` (the veil's colour, `#RRGGBB`; warm = amber). Built in: `day` (full), `evening` (full, 20 % dimmer, a little warmer) and `night` (clock, 65 % dimmer). `modes` adds more or replaces one by name.
  - Rules pick the mode. They run in order, a later rule that holds wins, and none holding means `default` (`day`). A rule is `{"mode": "night", "from": "23:00", "to": "06:30", "days": ["fri", "sat"]}`. Times are `HH:MM`, `sunrise`, `sunset`, or an offset like `sunset+30` or `sunrise-15` (minutes). A window past midnight belongs to the day it starts on. `days` are `mon`…`sun`, `weekdays` and `weekend`; none means every day. The defaults: evening from `sunset+30` to 23:00, night 23:00–06:30. `rules` replaces them; `extra_rules` is appended. Sun times come from `latitude` and `longitude`. A bad mode, time or day is a config error at start.
  - dashd sleeps until the exact next change, and wakes at least hourly, which also absorbs DST. Unchanged output is dropped, so Kodi is only notified on a real change. The TV view is `mode.name`, `mode.layout` and `mode.veil` (`AARRGGBB` for the veil's colordiffuse, `""` = none). `data` adds `dim`, `tint`, `source` (`schedule` or `override`), `until` (unix seconds), `next`, and every mode.
  - Set a mode from anywhere (a "Good night" button, couchd, a script): `POST /v1/widgets/mode {"op": "set", "mode": "night"}` holds until the schedule next changes; `"minutes": 90` sets a duration instead; `{"op": "auto"}` goes back to the schedule. The override is kept across restarts. Any POST also re-polls the widget, so an override ends on time.
  - On the TV, the night clock wanders across the screen every 4 min (`CLOCK_SHIFTS` in `dashboard.py`), since a still clock all night is the worst case for burn-in. Layouts and the veil fade over 1.5 s.
  - A new setting is a field on `Mode`, a view key in `output`, and its use in the skin; the skin contract test checks the keys. A new layout is a `Layout` variant plus a skin group shown for it.
- **frog**: one of ~7,900 Anura species on iNaturalist each day. The pick is a hash of the local date, so it is the same frog after a restart. It shows the Latin name, common name, family, IUCN status, a Wikipedia summary and a CC-licensed photo with its attribution; photos without a licence are skipped. The TV gets a shorter summary: whole sentences up to about 260 characters (five lines), without the opening "… is a species of frog in the family …". `data.summary` keeps the full text. The pick and its photo are saved, so the day costs three API calls. Param: `taxon_id` (default 20979, Anura).

## Adding a widget

1. `src/widgets/<kind>.rs`: a `build(&Value) -> Result<Box<dyn Widget>>` and an `impl Widget`. Implement only what applies:
   - **Polled** (a web API): `next_refresh` + `refresh`.
   - **Pushed** (the weather station POSTs readings): `handle`. Return the new `Output` and dashd stores it, bumps the version and notifies Kodi. Keep the last reading with `ctx.state` so a restart shows it.
   - **Stateful with commands** (like `board`): `handle` takes `{"op": ...}`, updates its state in `ctx.state`, and returns the whole output. `initial` loads it at start. `handle` runs on HTTP threads beside the scheduler, so keep the state behind a `Mutex`.
2. Add it to `KINDS` in `src/widgets/mod.rs` and to `dashd_widgets` in `ansible/group_vars/all.yml`.
3. For the TV: put its `view` keys in `screensaver-couchos-dashboard.xml` as `$INFO[Window.Property(<id>.<key>)]`, and add the widget to the fixture in `widgets::skin_contract`: that test fails when the skin binds a key dashd does not serve (an empty slot on the TV otherwise fails silently). The phone and web read `data` instead. A new widget goes in the bottom row as a card: a 570-wide group in its `grouplist`. Its `visible` condition says when it has a slot of its own and when it is a carousel slide (`String.IsEqual(Window.Property(slide),<name>)`, plus a line in `SLIDES` in `dashboard.py`); follow the next-days and app cards. Three slots fit.
4. Rebuild (`./build.sh`) before the playbook: it refuses a build older than `src/`.

```sh
cargo test                # unit tests, macOS or Linux
cargo run                 # serves the default dashboard on :8766 (DASHD_CONFIG=... for another)
./build.sh                # → dist/dashd-<version>-trixie-arm64 (Docker, arm64, Pi's glibc)
```

## Ideas for later

- **Home**: the weather station (indoor/outdoor temperature, humidity, CO₂, pressure trend) beside the forecast; "open a window" when CO₂ is high.
- **Getting around**: next trams and buses from the nearest stop (IDS BK / DPB departures), with "leave in 4 min".
- **Calendar**: today's events from a shared iCal/CalDAV feed; birthdays and name days (meniny: the Slovak name-day calendar).
- **Air and sky**: air quality (Open-Meteo's air-quality API: PM2.5, pollen in spring), UV index, sunrise and sunset as a day arc, moon phase.
- **From CouchOS itself**: "Continue watching" from the Plex index with a poster, newly added movies, the NAS disk filling up, a Pi under-voltage warning.
- **Photo of the day** from `/srv/media/Photos` ("on this day, years ago"), alternating with the frog.
- **Time of day, beyond the dashboard** (the `mode` widget is the one clock; these read it rather than keep their own times):
  - *Quiet hours*: no AirPlay auto-dashboard and no toasts at night; a volume cap (couchd sets Kodi's volume on a mode change).
  - *TV off at night*: CEC standby when the mode is `night` and nothing has played for an hour; `cec_on` in the morning only on a weekday, if wanted.
  - *Morning*: a `morning` layout (the duck big, umbrella or no umbrella, the next trams) from 06:30 to 08:30 on weekdays; trams only then (widget schedules, below).
  - *Per-mode carousel*: which bottom-row cards and slides show (e.g. no QR code in the evening), and a slower slide time at night.
  - *Per-mode screensaver delay*: Kodi's `screensaver.time` shorter at night, so the dim clock comes sooner.
  - *Movie mode*: `{"op": "set", "mode": "movie"}` from the app while something plays, or automatically when video plays after sunset, for a darker idle screen during pauses.
  - *The phone*: a "Good night" button (Settings → Dashboard) and the current mode shown there (`data.mode`, `data.until`).
- **Music**: now playing in the phone app and on the web page too (a pushed `nowplaying` widget, fed by the AirPlay add-on or by couchd from Kodi's JSON-RPC); a "music mode" with the artwork large and the rest small; lyrics or "what's this song" from the track name.
- **Plumbing**: SSE `/v1/events` for the app (a `Notifier`), widget schedules (e.g. trams only 6–9 am: polled widgets asking the `mode` widget, or a `when` on a widget spec using the same rule syntax), more carousel slides.
