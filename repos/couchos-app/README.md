# CouchOS remote (iOS)

Minimal SwiftUI remote for CouchOS. iOS 18+, no dependencies.

Open `CouchOS.xcodeproj` and set your team under Signing & Capabilities.
The bundle id is `dev.couchos.remote`; change it if you like. Then run it on your iPhone.

## How it talks to the box

| Channel | Transport | Used for |
|---|---|---|
| Kodi JSON-RPC | one persistent TCP socket to `:9090`, Nagle off | keys, typing, now playing, keyboard requests (pushed) |
| Kodi EventServer | UDP `:9777` | mouse pointer |
| couchd | HTTP keep-alive to `:8765` | screen mode, TV power, volume, Bluetooth |

The latency-critical path goes straight from the phone to Kodi: one small
write per key, no HTTP and no proxy hop. Haptics fire as the command is sent.
The box is found over Bonjour (`_couchos._tcp`) and resolved to an IPv4
address once, so later commands don't wait on mDNS lookups.

## Controls

- **Touchpad, swipe mode:** swipe = one step per 36 pt (keep dragging to scroll), tap = select, long press = context menu.
- **Touchpad, pointer mode:** drag moves Kodi's mouse cursor, tap = click (Kodi focuses what's under the cursor), long press = right click / context menu.
- **Keyboard:** opens by itself when Kodi asks for text (search, server address). Every keystroke is mirrored to the TV; Return confirms.
- **Sliders button:** screen mode (Plex / AirPlay / Off), TV power over CEC, volume, Bluetooth speakers, host override.
