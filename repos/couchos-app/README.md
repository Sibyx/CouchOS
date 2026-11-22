# CouchOS remote (iOS)

Minimal SwiftUI remote for CouchOS. iOS 18+, no dependencies.

Open `CouchOS.xcodeproj` and set your team under Signing & Capabilities.
The bundle id is `dev.couchos.remote`; change it if you like. Then run it on your iPhone.

## How it talks to the box

| Channel | Transport | Used for |
|---|---|---|
| Kodi JSON-RPC | one persistent TCP socket to `:9090`, Nagle off | keys, typing, now playing, keyboard requests (pushed) |
| Kodi EventServer | UDP `:9777` | mouse pointer |
| couchd | HTTP keep-alive to `:8765` | TV power, volume, Bluetooth, status (Server-Sent Events), Plex catalog and Play on TV |

The latency-critical path goes straight from the phone to Kodi: one small
write per key, no HTTP and no proxy hop. Haptics fire as the command is sent.
The box is found over Bonjour (`_couchos._tcp`) and resolved to an IPv4
address once, so later commands don't wait on mDNS lookups.

## Tabs

- **Browse:** the Plex catalog from couchd's index on the Pi: home rows (Continue Watching, Recently Added, ...), libraries as poster grids (sort, unwatched only), and search as you type (scopes: all, movies, shows, episodes; recent searches). A movie or episode has **Play on TV** / **Resume** / **Play from the start**; a show or season offers its next episode (**Resume S01E04**) and lists its children. The phone never streams: Plex on the TV plays the item and the Pi fetches it from the server. Playing something switches to the Remote tab. If AirPlay is on screen the app asks before playing over it.
  - **Long-press** any poster or row (or ⋯ on its screen): play, refresh from Plex, save or remove its artwork on the TV box, for it and everything under it. Pull down on a detail screen to refresh it.
  - The **drive button** (top right) shows the index's state (a spinner while syncing; a thin progress bar runs under the title) and opens it: libraries in or out of the index, keep artwork, check for changes, full sync, artwork cache, rebuild.
- **Remote:** the controls below.

## Controls

- **Touchpad, swipe mode:** swipe = one step per 36 pt (keep dragging to scroll), tap = select, long press = context menu.
- **Touchpad, pointer mode:** drag moves Kodi's mouse cursor, tap = click (Kodi focuses what's under the cursor), long press = right click / context menu.
- **Keyboard:** opens by itself when Kodi asks for text (search, server address). Every keystroke is mirrored to the TV; Return confirms.
- **Sliders button:** TV power over CEC, stop AirPlay, volume, Bluetooth speakers, host (found over Bonjour; a typed host stays until cleared).
