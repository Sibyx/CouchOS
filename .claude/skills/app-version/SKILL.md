---
name: app-version
description: Guards the CouchOS iOS app's build number and changelog. Use whenever you change anything that ships in the app under repos/couchos-app/ (Swift code, assets, Info.plist, build settings, the privacy manifest), before telling the user the change is done, and whenever the user says to bump, change or release the app version. Increments CURRENT_PROJECT_VERSION by exactly one per change, adds a CHANGELOG.md entry tagged with that build, changes MARKETING_VERSION only when the user says so, and keeps all of it valid for App Store Connect / TestFlight uploads.
---

# CouchOS app: build number and changelog

The app's version lives in one file, `repos/couchos-app/Config/Version.xcconfig`:

| Setting | Info.plist key | App Store Connect | Who changes it |
|---|---|---|---|
| `MARKETING_VERSION` (`X.Y.Z`) | `CFBundleShortVersionString` | Version | **Only the user.** Never on your own. |
| `CURRENT_PROJECT_VERSION` (integer) | `CFBundleVersion` | Build | You: **+1 for every change to the app** |

The changelog is `repos/couchos-app/CHANGELOG.md`. The tool is
`repos/couchos-app/scripts/release.py`, standard-library Python. It is the only
thing that edits those two settings. Never set them in the pbxproj or in Xcode's
target editor, because a target-level value overrides the xcconfig.

## Commands

Run each as its own Bash call, with the absolute path, and never behind `cd`:

```
python3 /Users/jdubec/Projects/CouchOS/repos/couchos-app/scripts/release.py show
python3 /Users/jdubec/Projects/CouchOS/repos/couchos-app/scripts/release.py bump
python3 /Users/jdubec/Projects/CouchOS/repos/couchos-app/scripts/release.py set-version X.Y.Z
python3 /Users/jdubec/Projects/CouchOS/repos/couchos-app/scripts/release.py check
python3 /Users/jdubec/Projects/CouchOS/repos/couchos-app/scripts/release.py notes [--since BUILD]
python3 /Users/jdubec/Projects/CouchOS/repos/couchos-app/scripts/release.py release [YYYY-MM-DD]
```

## When you change the app

"The app" means anything that ends up in the built `.app`:
`repos/couchos-app/CouchOS/**`, `Config/Info.plist`, and build settings in
`CouchOS.xcodeproj`. Docs, `scripts/` and the README are not the app, so they get
no bump. The same goes for changes under `couchd/`, `ansible/` and the rest of the
repo: they are not the iOS app.

Once per task (one user request), not once per file or per edit:

1. **Bump once.** Run `release.py bump`. If you already bumped earlier in the same
   task, do not bump again: one task is one build. A new user request that changes
   the app again is a new build, even if nothing was uploaded in between, because
   builds must only go up and skipping numbers is fine.
2. **Write the entry.** Add one line per user-visible change under the current
   version's section in `CHANGELOG.md`, in the right category, ending with the
   new build:

   ```
   ### Fixed
   - Search no longer drops the last letter typed. (build 7)
   ```

   - Categories are only `### Added`, `### Changed`, `### Fixed` and `### Removed`,
     in that order. Create the category heading if it is missing.
   - Write for a tester reading TestFlight's "What to Test": what they will notice,
     in plain words. Don't name files or functions. Keep each entry to one line.
   - A refactor with no visible effect still gets one short entry, so every build
     has one, for example `### Changed` `- Internal clean-up of the remote's
     networking; no visible change. (build 8)`.
3. **Check.** Run `release.py check`. It must print `✓`. Fix whatever it lists.
4. **Build.** A build number that doesn't build is useless. Run this as one call,
   with derived data in the session scratchpad:

   ```
   xcodebuild -project /Users/jdubec/Projects/CouchOS/repos/couchos-app/CouchOS.xcodeproj -scheme CouchOS -destination "generic/platform=iOS Simulator" -derivedDataPath <scratchpad>/dd build -quiet
   ```

   No output other than the line `IDERunDestination: Supported platforms for the
   buildables in the current scheme is empty` means it succeeded. That line is
   harmless, so don't chase it. Ignore SourceKit "Cannot find 'X' in scope" editor
   diagnostics too: they come from checking one file on its own, and only
   xcodebuild counts.
5. **Tell the user** the new `X.Y.Z (N)` in your summary.

Never edit or renumber an existing entry's `(build N)`. Never lower the build.
Never reuse a number.

## When the user asks to change the version

Only when the user explicitly says to, for example "bump to 0.2.0", "this is
1.0", "new minor version" or "release a patch".

1. Settle the number. Follow [semver](https://semver.org): patch `X.Y.Z+1` for
   fixes only, minor `X.Y+1.0` for new features, major `X+1.0.0` for a breaking
   change or the 1.0 launch. If the user names a kind ("minor") instead of a
   number, work out the number and state it. If it's ambiguous, ask.
2. Run `release.py set-version X.Y.Z`. It refuses a version that is not higher,
   and opens a `## [X.Y.Z] — unreleased` section at the top of the changelog.
3. Run `release.py bump`. A new version always starts with a new build.
4. Add the entries for this task under the new section, then run `release.py check`.

When the user says a version went **live on the App Store** (not just
TestFlight), run `release.py release` to date its section. The next change then
needs `set-version` first, because `check` fails if the newest section isn't the
app's version. Ask the user which version comes next.

## App Store Connect rules this protects

- The Version is one to three period-separated integers. We always use three.
- The Build must be higher than every build uploaded before for that version, or
  the upload is rejected as a duplicate. We keep one counter that never resets,
  which also satisfies the rule across versions.
- The "What to Test" and "What's New" text is limited to 4,000 characters. Make it
  with `release.py notes`, or `notes --since <last uploaded build>` to list only
  what's new since the last upload. The command fails when the text is over the limit.
- `ITSAppUsesNonExemptEncryption = NO` stays in `Config/Info.plist`, and
  `CouchOS/PrivacyInfo.xcprivacy` must list every required-reason API the app
  uses. If you add one (file timestamps, disk space, system boot time, or a new
  UserDefaults suite), add its reason to the manifest in the same change.

## Don't

- Don't run `agvtool`, and don't edit `Version.xcconfig` by hand. Use `release.py`.
- Don't bump for changes outside the app (docs, `couchd/`, `ansible/`).
- Don't commit unless the user asks. When they do, the version files and the
  changelog go in the same commit as the change.
