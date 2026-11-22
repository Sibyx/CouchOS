import Foundation
import Observation
import UIKit

enum Key: String, CaseIterable {
    case up, down, left, right, select, back, home, info, menu
    case playPause, stop, forward, rewind

    /// JSON-RPC call for this key on Kodi's TCP link.
    var rpc: (method: String, params: [String: Any]) {
        switch self {
        case .up: ("Input.Up", [:])
        case .down: ("Input.Down", [:])
        case .left: ("Input.Left", [:])
        case .right: ("Input.Right", [:])
        case .select: ("Input.Select", [:])
        case .back: ("Input.Back", [:])
        case .home: ("Input.Home", [:])
        case .info: ("Input.Info", [:])
        case .menu: ("Input.ContextMenu", [:])
        case .playPause: ("Input.ExecuteAction", ["action": "playpause"])
        case .stop: ("Input.ExecuteAction", ["action": "stop"])
        case .forward: ("Input.ExecuteAction", ["action": "stepforward"])
        case .rewind: ("Input.ExecuteAction", ["action": "stepback"])
        }
    }
}

/// Things the TV can show. Plex is PM4K (it reactivates itself if already
/// open) and is the TV's home: the Home button opens it. The rest are Kodi's
/// own windows, offered under "Open on TV".
enum TVApp: String, CaseIterable, Identifiable {
    case plex, photos, music, kodi
    var id: String { rawValue }

    static let secondary: [TVApp] = [.photos, .music, .kodi]

    var title: String {
        switch self {
        case .plex: "Plex"
        case .photos: "Photos"
        case .music: "Music"
        case .kodi: "Kodi library"
        }
    }

    var symbol: String {
        switch self {
        case .plex: "play.tv"
        case .photos: "photo.on.rectangle"
        case .music: "music.note"
        case .kodi: "square.grid.2x2"
        }
    }

    var rpc: (method: String, params: [String: Any]) {
        switch self {
        case .plex: ("Addons.ExecuteAddon", ["addonid": "script.plexmod"])
        case .photos: ("GUI.ActivateWindow", ["window": "pictures"])
        case .music: ("GUI.ActivateWindow", ["window": "music"])
        case .kodi: ("GUI.ActivateWindow", ["window": "home"])
        }
    }
}

struct NowPlaying: Equatable {
    var title: String
    var subtitle: String
    var paused: Bool
    /// couchd plays AirPlay as tcp://127.0.0.1:5010/airplay.ts inside Kodi.
    var isAirPlay = false
}

enum AppTab: Hashable { case browse, remote }

@Observable
final class AppModel {
    /// Browse (the Plex catalog) or Remote; playing something switches to Remote.
    var tab: AppTab = .browse

    // Connection
    /// The TV box's address. Empty until Bonjour finds one (or one is typed in).
    var host: String {
        didSet { UserDefaults.standard.set(host, forKey: "host") }
    }
    private(set) var kodiState: KodiLink.State = .idle
    /// iOS refuses Local Network access: nothing can be found or reached
    /// until it is allowed in Settings.
    private(set) var localNetworkDenied = false
    /// The TV, sound, Bluetooth and device sheet (from Remote, or the connection help).
    var showSettings = false

    // State shown in the UI
    private(set) var nowPlaying: NowPlaying?
    private(set) var status: Status?
    private(set) var audio: Audio?
    private(set) var bluetooth: BluetoothState?
    /// What the TV has selected right now ("Home · Movies"), when Kodi can
    /// tell. PM4K's own windows often report nothing; then this stays nil.
    private(set) var focus: String?
    var keyboard: KeyboardRequest?
    var message: String?

    struct KeyboardRequest: Identifiable {
        let id = UUID()
        var title: String
        var text: String
    }

    /// A phone is mirroring to the TV (Kodi plays the AirPlay stream on top of Plex).
    var airplayActive: Bool {
        nowPlaying?.isAirPlay == true || status?.airplay.active == true
    }

    /// screen (mirroring), video (from an app) or audio, as couchd sees it.
    var airplayKind: String? { status?.airplay.kind }

    private let kodi = KodiLink()
    private let discovery = Discovery()
    private var flick: Task<Void, Never>?
    private let tap = UIImpactFeedbackGenerator(style: .light)
    private let thud = UIImpactFeedbackGenerator(style: .medium)
    private var poll: Task<Void, Never>?
    private var focusQuery: Task<Void, Never>?
    /// The host the links were last opened to.
    private var connectedHost: String?

    init() {
        host = UserDefaults.standard.string(forKey: "host") ?? ""
        kodi.onState = { [weak self] state in self?.kodiStateChanged(state) }
        kodi.onNotification = { [weak self] method, params in self?.kodiNotification(method, params) }
        discovery.onAccess = { [weak self] allowed in self?.localNetworkDenied = !allowed }
        discovery.onFound = { [weak self] address in
            guard let self else { return }
            self.localNetworkDenied = false
            // A host typed in by hand stays until it is cleared.
            guard !self.hostPinned, address != self.host else { return }
            self.host = address
            self.reconnect()
        }
    }

    /// The control plane, once there is a box to talk to.
    var couchd: Couchd? { host.isEmpty ? nil : Couchd(host: host) }

    /// No box yet: nothing found over Bonjour and nothing typed in.
    var hasHost: Bool { !host.isEmpty }

    /// Set when the host was typed in (More → Device); Bonjour then leaves it alone.
    private(set) var hostPinned = UserDefaults.standard.bool(forKey: "hostPinned") {
        didSet { UserDefaults.standard.set(hostPinned, forKey: "hostPinned") }
    }

    /// Use this host from now on, or (empty) go back to finding the box over Bonjour.
    func pinHost(_ text: String) {
        let text = text.trimmingCharacters(in: .whitespaces)
        hostPinned = !text.isEmpty
        host = text.isEmpty ? (discovery.found ?? "") : text
        reconnect()
    }

    // MARK: lifecycle

    func activate() {
        tap.prepare()
        discovery.start()
        reconnect()
    }

    func deactivate() {
        poll?.cancel()
        discovery.stop()
        kodi.disconnect()
    }

    /// (Re)open both links to the current host: Kodi's socket and couchd's
    /// status stream. With no host yet, wait for Bonjour.
    func reconnect() {
        poll?.cancel()
        if connectedHost != host {
            // Another box (or none): what was shown belongs to the old one.
            // Coming back to the app on the same box keeps it, so nothing flickers.
            status = nil
            audio = nil
            nowPlaying = nil
            connectedHost = host
        }
        guard hasHost, let couchd else {
            kodi.disconnect()
            return
        }
        kodi.connect(host: host)
        poll = Task { [weak self] in
            // couchd pushes a status snapshot on every change (Server-Sent
            // Events); no polling. Reconnect with backoff if it drops.
            var backoff = 1.0
            while !Task.isCancelled {
                do {
                    for try await status in couchd.events() {
                        self?.status = status
                        backoff = 1
                    }
                } catch {
                    if Task.isCancelled { return }
                }
                await self?.refreshStatus()
                try? await Task.sleep(for: .seconds(backoff))
                backoff = min(backoff * 2, 15)
            }
        }
    }

    // MARK: Kodi (low-latency path)

    func press(_ key: Key) {
        flick?.cancel()
        tap.impactOccurred()
        tap.prepare()
        let rpc = key.rpc
        kodi.call(rpc.method, rpc.params)
        refreshFocusSoon()
    }

    /// Plex is the TV's home (PM4K kiosk); Kodi's own home is under "Open on TV".
    func goHome() {
        open(.plex)
    }

    func open(_ app: TVApp) {
        thud.impactOccurred()
        let rpc = app.rpc
        kodi.call(rpc.method, rpc.params) { [weak self] result in
            if result == nil { self?.message = "The TV didn't open \(app.title). Is Kodi running?" }
        }
        refreshFocusSoon()
    }

    /// Ask Kodi what is selected once the key presses settle (debounced, so a
    /// swipe does not turn into a query per step).
    private func refreshFocusSoon() {
        focusQuery?.cancel()
        focusQuery = Task { [weak self] in
            try? await Task.sleep(for: .milliseconds(250))
            guard !Task.isCancelled else { return }
            self?.refreshFocus()
        }
    }

    private func refreshFocus() {
        let labels = ["System.CurrentWindow", "System.CurrentControl", "ListItem.Label"]
        kodi.call("XBMC.GetInfoLabels", ["labels": labels]) { [weak self] result in
            guard let self, let values = result as? [String: String] else { return }
            let window = values["System.CurrentWindow"] ?? ""
            let item = [values["ListItem.Label"], values["System.CurrentControl"]]
                .compactMap { $0?.trimmingCharacters(in: .whitespaces) }
                .first { !$0.isEmpty }
            // "System" is what an add-on window (PM4K) reports: no useful name.
            let place = window.isEmpty || window == "System" ? nil : window
            focus = [place, item].compactMap { $0 }.joined(separator: " · ").nilIfEmpty
        }
    }

    /// Momentum after a fast swipe: extra steps that slow down, like a list
    /// coasting. Any new touch cancels it.
    func coast(_ key: Key, steps: Int) {
        flick?.cancel()
        guard steps > 0 else { return }
        let rpc = key.rpc
        flick = Task { [weak self] in
            var delay = 45.0
            for _ in 0..<steps {
                try? await Task.sleep(for: .milliseconds(Int(delay)))
                guard !Task.isCancelled, let self else { return }
                self.kodi.call(rpc.method, rpc.params)
                delay *= 1.18
            }
        }
    }

    func cancelCoast() {
        flick?.cancel()
    }

    func sendText(_ text: String, done: Bool) {
        kodi.call("Input.SendText", ["text": text, "done": done])
    }

    private func kodiStateChanged(_ state: KodiLink.State) {
        kodiState = state
        if state == .ready {
            refreshNowPlaying()
            refreshFocus()
        } else {
            focus = nil
        }
    }

    private func kodiNotification(_ method: String, _ params: [String: Any]) {
        switch method {
        case "Player.OnPlay", "Player.OnAVStart", "Player.OnResume", "Player.OnPause", "Player.OnStop":
            refreshNowPlaying()
            refreshFocusSoon()
        case "Input.OnInputRequested":
            let data = params["data"] as? [String: Any] ?? [:]
            keyboard = KeyboardRequest(title: data["title"] as? String ?? "Type", text: data["value"] as? String ?? "")
        case "Input.OnInputFinished":
            keyboard = nil
        default:
            break
        }
    }

    private func refreshNowPlaying() {
        kodi.call("Player.GetActivePlayers") { [weak self] result in
            guard let self else { return }
            guard let player = (result as? [[String: Any]])?.first, let id = player["playerid"] as? Int else {
                self.nowPlaying = nil
                return
            }
            self.loadItem(player: id)
        }
    }

    private func loadItem(player id: Int) {
        kodi.call("Player.GetItem", ["playerid": id, "properties": ["title", "showtitle", "season", "episode", "artist", "file"]]) { [weak self] result in
            guard let self, let item = (result as? [String: Any])?["item"] as? [String: Any] else {
                self?.nowPlaying = nil
                return
            }
            let title = (item["title"] as? String).flatMap { $0.isEmpty ? nil : $0 } ?? (item["label"] as? String ?? "")
            var subtitle = ""
            if let show = item["showtitle"] as? String, !show.isEmpty {
                let season = item["season"] as? Int ?? -1
                let episode = item["episode"] as? Int ?? -1
                subtitle = season >= 0 ? String(format: "%@ · S%02dE%02d", show, season, episode) : show
            } else if let artists = item["artist"] as? [String], !artists.isEmpty {
                subtitle = artists.joined(separator: ", ")
            }
            // inputstream.airplay plays mirroring as airplay://mirror; the
            // UxPlay bridge (airplay_receiver: uxplay) as tcp://.../airplay.ts.
            let file = item["file"] as? String ?? ""
            if file.hasPrefix("airplay://") || file.hasSuffix("/airplay.ts") {
                self.nowPlaying = NowPlaying(title: "iPhone screen", subtitle: "AirPlay", paused: false, isAirPlay: true)
                return
            }
            self.kodi.call("Player.GetProperties", ["playerid": id, "properties": ["speed"]]) { props in
                let paused = ((props as? [String: Any])?["speed"] as? Int) == 0
                self.nowPlaying = title.isEmpty ? nil : NowPlaying(title: title, subtitle: subtitle, paused: paused)
            }
        }
    }

    // MARK: Plex on the TV

    /// Hand a Plex item to the TV: Plex there plays it (movies, episodes) or
    /// opens its screen (shows, seasons). The Pi streams it from the server,
    /// with Plex's own transcoding, resume and watched state.
    func playOnTV(_ item: PlexItem, fromStart: Bool = false, replace: Bool = false) {
        thud.impactOccurred()
        guard let couchd else { return }
        Task {
            do {
                let _: OK = try await couchd.post("plex/play", [
                    "key": item.key, "play": item.isPlayable, "from_start": fromStart, "replace": replace,
                ])
                if item.isPlayable { tab = .remote }
            } catch CouchdError.conflict {
                // AirPlay is on screen: ask instead of cutting the phone off.
                playOverAirPlay = PendingPlay(item: item, fromStart: fromStart)
            } catch {
                message = error.localizedDescription
            }
        }
    }

    struct PendingPlay: Identifiable {
        let id = UUID()
        let item: PlexItem
        let fromStart: Bool
    }

    /// Set when Play was refused because AirPlay is on screen.
    var playOverAirPlay: PendingPlay?

    // MARK: the index and artwork cache on the Pi

    /// A short confirmation shown over the content ("Refreshing Severance").
    private(set) var toast: String?
    private var toastTask: Task<Void, Never>?

    func show(toast text: String) {
        toast = text
        toastTask?.cancel()
        toastTask = Task { [weak self] in
            try? await Task.sleep(for: .seconds(2.2))
            guard !Task.isCancelled else { return }
            self?.toast = nil
        }
    }

    enum CatalogAction {
        case refresh, fetchArt, dropArt

        func toast(_ title: String) -> String {
            switch self {
            case .refresh: "Refreshing \(title) from Plex"
            case .fetchArt: "Saving artwork for \(title)"
            case .dropArt: "Removing saved artwork for \(title)"
            }
        }
    }

    /// Per entry, for it and everything under it (a show's seasons and episodes).
    func catalog(_ action: CatalogAction, _ item: PlexItem) {
        tap.impactOccurred()
        show(toast: action.toast(item.title))
        run { api in
            switch action {
            case .refresh: try await api.catalogRefresh(item.key)
            case .fetchArt: try await api.catalogArt(item: item.key, fetch: true)
            case .dropArt: try await api.catalogArt(item: item.key, fetch: false)
            }
        }
    }

    // MARK: couchd (control plane)

    func refreshStatus() async {
        guard let couchd else { return }
        status = try? await couchd.get("status")
        audio = try? await couchd.get("audio")
    }

    func refreshBluetooth() async {
        bluetooth = try? await couchd?.get("bluetooth")
    }

    func stopAirPlay() {
        thud.impactOccurred()
        run { [self] api in
            let _: OK = try await api.post("airplay/stop")
            nowPlaying = nil
            await refreshStatus()
        }
    }

    func tv(_ power: String) {
        thud.impactOccurred()
        run { let _: OK = try await $0.post("tv", ["power": power]) }
    }

    /// Clock, weather and the frog of the day on the TV now (it is also the
    /// TV's idle screen). Wakes the TV; any key goes back to Plex.
    func showDashboard() {
        thud.impactOccurred()
        guard let couchd else { return }
        Task {
            do {
                let _: OK = try await couchd.post("dashboard", ["show": true])
            } catch CouchdError.conflict {
                message = "A video is playing on the TV. Stop it first, then open the dashboard."
            } catch {
                message = error.localizedDescription
            }
        }
    }

    func volume(delta: Int) {
        tap.impactOccurred()
        run { [self] api in audio = try await api.post("audio", ["delta": delta]) }
    }

    func toggleMute() {
        run { [self] api in audio = try await api.post("audio", ["mute": !(audio?.muted ?? false)]) }
    }

    func restartKodi() {
        run { let _: OK = try await $0.post("kodi/restart") }
    }

    func scanBluetooth() {
        run { [self] api in
            let _: OK = try await api.post("bluetooth/scan")
            for _ in 0..<8 {
                try await Task.sleep(for: .seconds(2.5))
                await refreshBluetooth()
            }
        }
    }

    func bluetooth(_ action: String, _ device: BluetoothState.Device) {
        run { [self] api in
            let _: OK = try await api.post("bluetooth/\(action)", ["mac": device.mac])
            await refreshBluetooth()
        }
    }

    func run(_ work: @escaping (Couchd) async throws -> Void) {
        guard let couchd else { return }
        Task {
            do { try await work(couchd) } catch { message = error.localizedDescription }
        }
    }
}

private extension String {
    var nilIfEmpty: String? { isEmpty ? nil : self }
}
