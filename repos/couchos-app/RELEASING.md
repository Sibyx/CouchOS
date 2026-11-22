# Releasing the CouchOS app (TestFlight → App Store)

The version and build live in `Config/Version.xcconfig`. The changelog is
`CHANGELOG.md`, and `scripts/release.py` keeps the two consistent. Claude Code
does this automatically through the `app-version` skill
(`/.claude/skills/app-version/`).

| | Where | Rule |
|---|---|---|
| Version `X.Y.Z` | `MARKETING_VERSION` | Semantic versioning. Changes only when decided. |
| Build `N` | `CURRENT_PROJECT_VERSION` | +1 for every change to the app. Never resets. |

```sh
python3 scripts/release.py show      # 0.1.0 (1)
python3 scripts/release.py check     # must print ✓ before every upload
```

## One-time setup

1. **App ID.** Xcode's automatic signing registers `dev.dubec.couchos` on the
   first archive. You can also register it yourself in Certificates, Identifiers &
   Profiles. It needs no extra capabilities: Local Network and Bonjour are only
   Info.plist keys.
2. **App record.** In App Store Connect, go to Apps → **+** → New App:
   - Platform: iOS. Name: **CouchOS**. App Store names are unique, so if it's
     taken, use "CouchOS Remote". The name on the home screen stays CouchOS.
   - Primary language: English. Bundle ID: `dev.dubec.couchos`. SKU: `couchos-ios`.
3. **Privacy.** In App Privacy, the answer is "Data Not Collected": the app only
   talks to the user's own box on the local network. Host `docs/PRIVACY.md` at
   `https://couchos.dubec.dev/privacy` and enter that URL.

## Every upload

1. Run `python3 scripts/release.py check`, which must print ✓.
2. Archive and upload, either from Xcode (Product → Archive → Distribute App →
   App Store Connect → Upload) or from the shell:

   ```sh
   xcodebuild -project CouchOS.xcodeproj -scheme CouchOS -configuration Release \
     -destination generic/platform=iOS -archivePath build/CouchOS.xcarchive archive
   xcodebuild -exportArchive -archivePath build/CouchOS.xcarchive \
     -exportOptionsPlist Config/ExportOptions.plist -exportPath build/export
   ```

   Uploading from the shell needs Xcode signed in to the developer account, or an
   App Store Connect API key (`-authenticationKeyPath`, `-authenticationKeyID`,
   `-authenticationKeyIssuerID`). `ExportOptions.plist` sets
   `manageAppVersionAndBuildNumber` to false, so Xcode never renumbers the build.
3. **Export compliance** is answered already: `ITSAppUsesNonExemptEncryption = NO`
   is set, because the app uses plain HTTP and TCP on the LAN and no encryption of
   its own. The build skips the "Missing Compliance" step.
4. **What to Test:** paste the output of
   `python3 scripts/release.py notes --since <previous uploaded build>`
   (or `notes` for everything in this version). It fails if the text is over
   App Store Connect's 4,000-character limit.

## Beta testers and the invitation link

TestFlight has two kinds of groups:

| | Internal | External |
|---|---|---|
| Who | Up to 100 people on your App Store Connect team | Anyone (up to 10,000), by email or **public link** |
| Review | None. Builds are available right after processing. | **Beta App Review** before testers get the build |
| Link | No | Yes: TestFlight → the group → **Public Link**, optionally with a tester limit |

**For an invitation link,** create an external group, add the build, fill in
Test Information (below) and submit it for review. Once it's approved, turn on
Public Link for the group and share it. Apple reviews the first build of a
version; later builds of the same version often go through without a full
review.

### The catch: reviewers have no CouchOS box

The app does nothing without a CouchOS box on the same network, and the reviewer
won't have one. Expect them to see "Looking for your CouchOS box". Without an
explanation, that can read as a non-functional app. To reduce the risk:

- Put the explanation below in **Review Notes**.
- Attach or link a **short screen recording** of the app driving a real box: Browse,
  then Play on TV, then the remote. That's the single most useful thing to give the reviewer.
- If it's rejected anyway, there are two options. One is to use internal testing
  only (no review, but testers must be added to the App Store Connect team). The
  other is to add a demo mode with sample content that works without a box, which
  is a larger change.

### Test Information (copy and paste)

**Beta App Description**

> CouchOS is the remote for a CouchOS TV box: a Raspberry Pi that runs Plex,
> AirPlay and a dashboard on your TV. Browse your Plex library on the phone and
> play anything on the TV, use the phone as a touchpad remote, type into the TV
> from the phone's keyboard, and control TV power, volume and Bluetooth speakers.
> Requires a CouchOS box on the same Wi-Fi.

**Feedback email:** an address you read. TestFlight screenshots and feedback also
arrive in App Store Connect.

**Marketing URL:** `https://couchos.dubec.dev` · **Privacy Policy URL:**
`https://couchos.dubec.dev/privacy`

**Beta App Review information**

- Sign-in required: **No**. The app has no accounts.
- Review Notes:

> CouchOS is a companion remote for a self-built TV box (a Raspberry Pi
> running CouchOS: Kodi with Plex, an AirPlay receiver and a small control
> service). The app finds the box on the local network over Bonjour
> (_couchos._tcp) and controls it over the LAN. It has no account, no server
> and no internet backend, and collects no data.
>
> Without a CouchOS box on the same Wi-Fi network, the app shows "Looking for
> your CouchOS box" with help on what to check. That is the expected behaviour
> and not a crash. The attached video shows the app with a box: browsing the
> Plex library, starting playback on the TV, and using the touchpad remote.
>
> The Local Network permission is used only to find and talk to the box.

## When a version ships on the App Store

```sh
python3 scripts/release.py release            # dates the version's changelog section
python3 scripts/release.py set-version 0.2.0  # the next version, when decided
python3 scripts/release.py bump
```
