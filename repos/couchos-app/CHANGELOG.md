# Changelog

All notable changes to the CouchOS iOS app.

- **Version** (`X.Y.Z`, [Semantic Versioning](https://semver.org)) is the App Store
  "Version" (`CFBundleShortVersionString`). It changes only when a version bump is decided.
- **Build** is the App Store "Build" (`CFBundleVersion`): one integer that goes up by 1
  with every change and never resets. Each entry ends with the build that first had it.
- Sections are `## [X.Y.Z] — unreleased` until the version ships on the App Store, then
  dated. Categories: Added, Changed, Fixed, Removed.
- Maintained with `scripts/release.py`. See [RELEASING.md](RELEASING.md).

## [0.1.0] — unreleased

### Added
- Browse tab: the Plex catalog from the TV box's index, with home rows (Continue Watching, Recently Added), library poster grids with sort and "unwatched only", and search as you type with scopes and recent searches. (build 1)
- Play on TV, Resume and Play from the start for movies and episodes; a show or season offers its next episode. Plex on the TV plays it, and the phone never streams. (build 1)
- Long-press any poster or row to play it, refresh it from Plex, or save or remove its artwork on the box. (build 1)
- Library index screen: sync state and progress, libraries in or out of the index, keep artwork, check for changes, full sync, artwork cache, rebuild. (build 1)
- Remote tab: Apple TV-style touchpad (swipe, momentum, tap to select, hold for menu), Back, Home (Plex), Menu, keyboard, transport and volume buttons. (build 1)
- The keyboard opens by itself when the TV asks for text and mirrors every keystroke. (build 1)
- AirPlay banner with Stop. Playing over AirPlay asks first. (build 1)
- Settings sheet: TV power over HDMI-CEC, volume and mute, Bluetooth speakers, and a manually entered box address. (build 1)
- The box is found automatically over Bonjour (`_couchos._tcp`). (build 1)
- CouchOS brand: app icon, amber-on-ink colour tokens shared with the web dashboard, dark launch screen. (build 1)
- Connection help when no box is found, or when Local Network access is off (with a button to Settings). (build 1)
- About section with version and build, and how to send beta feedback. (build 1)
- Privacy manifest and export-compliance declaration, needed for TestFlight. (build 1)
- Dashboard screen (Settings → Dashboard): send a message to the TV, keep a shared to-do list on it, and see the weather duck the TV is showing. (build 2)
- Open on TV → CouchOS search: opens the TV's own catalog, for searching with the TV remote. (build 3)
- Dashboard → Reminders: show a Reminders list on the TV's to-do list. It stays in step while the app is open, and ticking an item in the app ticks it in Reminders. (build 6)
- Siri and Shortcuts actions: Show Dashboard on TV, Turn TV On or Off, Set Dashboard Mode (e.g. "Good night"), and Sync Reminders to TV. They also work in Shortcuts automations. (build 6)

### Changed
- Open on TV → CouchOS search opens at once: the TV keeps the catalog ready instead of starting it. (build 4)
- Dashboard messages no longer have a "From" field; the TV shows the message as a banner across the top of its dashboard. (build 4)
- The dashboard message note now matches the TV, where the message shows as a large yellow card in the bottom row. (build 5)

### Changed
- Bundle identifier is `dev.dubec.couchos`. (build 1)
- No built-in default box address: until a box is found, the app says it is looking. (build 1)

### Fixed
- Errors now show wherever you are: on either tab, and in the settings sheet (before, only on Remote). (build 1)- Browse no longer shows a spinner forever when no box has been found; it loads as soon as one is. (build 1)
