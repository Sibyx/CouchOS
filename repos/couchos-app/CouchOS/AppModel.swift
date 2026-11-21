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
/// open); the rest are Kodi's own windows.
enum TVApp: String, CaseIterable, Identifiable {
    case plex, photos, music, kodi
    var id: String { rawValue }

    var title: String {
        switch self {
        case .plex: "Plex"
        case .photos: "Photos"
        case .music: "Music"
        case .kodi: "Kodi"
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
    /// couchd plays AirPlay as udp://127.0.0.1:5010/airplay.ts inside Kodi.
    var isAirPlay = false
}

@Observable
final class AppModel {
    // Connection
    var host: String {
        didSet { UserDefaults.standard.set(host, forKey: "host") }
    }
    private(set) var kodiState: KodiLink.State = .idle

    // State shown in the UI
    private(set) var nowPlaying: NowPlaying?
    private(set) var status: Status?
    private(set) var audio: Audio?
    private(set) var bluetooth: BluetoothState?
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

    private let kodi = KodiLink()
    private let discovery = Discovery()
    private var flick: Task<Void, Never>?
    private let tap = UIImpactFeedbackGenerator(style: .light)
    private let thud = UIImpactFeedbackGenerator(style: .medium)
    private var poll: Task<Void, Never>?

    init() {
        host = UserDefaults.standard.string(forKey: "host") ?? "beelzebub-tv.local"
        kodi.onState = { [weak self] state in self?.kodiStateChanged(state) }
        kodi.onNotification = { [weak self] method, params in self?.kodiNotification(method, params) }
        discovery.onFound = { [weak self] address in
            guard let self, address != self.host else { return }
            self.host = address
            self.reconnect()
        }
    }

    var couchd: Couchd? { Couchd(host: host) }

    // MARK: lifecycle

    func activate() {
        tap.prepare()
        discovery.start()
        reconnect()
        poll?.cancel()
        poll = Task { [weak self] in
            while !Task.isCancelled {
                await self?.refreshStatus()
                try? await Task.sleep(for: .seconds(4))
            }
        }
    }

    func deactivate() {
        poll?.cancel()
        discovery.stop()
        kodi.disconnect()
    }

    func reconnect() {
        kodi.connect(host: host)
    }

    // MARK: Kodi (low-latency path)

    func press(_ key: Key) {
        flick?.cancel()
        tap.impactOccurred()
        tap.prepare()
        let rpc = key.rpc
        kodi.call(rpc.method, rpc.params)
    }

    func open(_ app: TVApp) {
        thud.impactOccurred()
        let rpc = app.rpc
        kodi.call(rpc.method, rpc.params) { [weak self] result in
            if result == nil { self?.message = "The TV didn't open \(app.title). Is Kodi running?" }
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
        if state == .ready { refreshNowPlaying() }
    }

    private func kodiNotification(_ method: String, _ params: [String: Any]) {
        switch method {
        case "Player.OnPlay", "Player.OnAVStart", "Player.OnResume", "Player.OnPause", "Player.OnStop":
            refreshNowPlaying()
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
            if (item["file"] as? String)?.hasSuffix("/airplay.ts") == true {
                self.nowPlaying = NowPlaying(title: "iPhone screen", subtitle: "AirPlay", paused: false, isAirPlay: true)
                return
            }
            self.kodi.call("Player.GetProperties", ["playerid": id, "properties": ["speed"]]) { props in
                let paused = ((props as? [String: Any])?["speed"] as? Int) == 0
                self.nowPlaying = title.isEmpty ? nil : NowPlaying(title: title, subtitle: subtitle, paused: paused)
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

    private func run(_ work: @escaping (Couchd) async throws -> Void) {
        guard let couchd else { return }
        Task {
            do { try await work(couchd) } catch { message = error.localizedDescription }
        }
    }
}
