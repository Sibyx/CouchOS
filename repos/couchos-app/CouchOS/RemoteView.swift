import SwiftUI

struct RemoteView: View {
    @Environment(AppModel.self) private var model

    var body: some View {
        @Bindable var model = model
        VStack(spacing: 14) {
            header
            if model.hasHost {
                if model.airplayActive { airplayBanner } else { nowPlaying }
                Touchpad()
            } else {
                ConnectionHelp()
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
                    .background(Brand.panel, in: RoundedRectangle(cornerRadius: Brand.Radius.large, style: .continuous))
            }
            controls
            transport
        }
        .padding(.horizontal, Brand.gutter)
        .padding(.bottom, 8)
        .background(Brand.ink.ignoresSafeArea())
        .animation(.snappy(duration: 0.2), value: model.airplayActive)
        .animation(.snappy(duration: 0.2), value: model.hasHost)
        .sheet(item: $model.keyboard) { request in KeyboardSheet(request: request) }
    }

    private var header: some View {
        HStack(spacing: 8) {
            HStack(spacing: 6) {
                Circle().fill(dotColor).frame(width: 7, height: 7)
                Text(connectionLabel).font(.footnote).foregroundStyle(Brand.muted).lineLimit(1)
            }
            .padding(.horizontal, 10).padding(.vertical, 5)
            .background(Brand.panel, in: Capsule())
            .accessibilityElement(children: .combine)
            Spacer()
            Menu {
                Section("Open on TV") {
                    Button { model.showDashboard() } label: { Label("Dashboard", systemImage: "rectangle.3.group") }
                    ForEach(TVApp.secondary) { app in
                        Button { model.open(app) } label: { Label(app.title, systemImage: app.symbol) }
                    }
                }
            } label: {
                Image(systemName: "square.grid.2x2").font(.title3).frame(width: 44, height: 44)
            }
            .accessibilityLabel("Open on TV")
            Button { model.showSettings = true } label: {
                Image(systemName: "slider.horizontal.3").font(.title3).frame(width: 44, height: 44)
            }
            .accessibilityLabel("TV, sound and Bluetooth")
        }
    }

    /// What plays, or else what the TV has selected, so the remote isn't blind.
    private var nowPlaying: some View {
        HStack {
            VStack(alignment: .leading, spacing: 2) {
                if let playing = model.nowPlaying {
                    Text(playing.title).font(.headline).lineLimit(1)
                    Text(playing.subtitle.isEmpty ? " " : playing.subtitle)
                        .font(.subheadline).foregroundStyle(Brand.muted).lineLimit(1)
                } else {
                    Text("On the TV").font(.caption).foregroundStyle(Brand.muted)
                    Text(model.focus ?? "Plex").font(.headline).lineLimit(1)
                        .contentTransition(.opacity)
                }
            }
            Spacer()
        }
        .frame(minHeight: 48)
        .animation(.easeOut(duration: 0.15), value: model.focus)
        .accessibilityElement(children: .combine)
    }

    private var airplayBanner: some View {
        HStack(spacing: 12) {
            Image(systemName: model.airplayKind == "audio" ? "airplayaudio" : "airplayvideo").font(.title2)
            VStack(alignment: .leading, spacing: 2) {
                Text(airplayTitle).font(.headline).lineLimit(1)
                Text("Stop to return to Plex").font(.subheadline).opacity(0.8).lineLimit(1)
            }
            Spacer()
            Button("Stop") { model.stopAirPlay() }
                .buttonStyle(.borderedProminent)
                .tint(Brand.onAmber.opacity(0.35))
        }
        .foregroundStyle(Brand.onAmber)
        .padding(12)
        .frame(minHeight: 48)
        .background(RoundedRectangle(cornerRadius: Brand.Radius.medium, style: .continuous).fill(Brand.amber))
    }

    private var airplayTitle: String {
        switch model.airplayKind {
        case "audio": "AirPlay music"
        case "video": "AirPlay video"
        default: "Screen sharing"
        }
    }

    private var controls: some View {
        HStack(spacing: 12) {
            RemoteButton(symbol: "arrow.uturn.backward", label: "Back") { model.press(.back) }
            // Home is Plex (the TV's home screen), not Kodi's own home.
            RemoteButton(symbol: "house", label: "Home (Plex)") { model.goHome() }
            RemoteButton(symbol: "ellipsis", label: "Menu") { model.press(.menu) }
            RemoteButton(symbol: "keyboard", label: "Keyboard") {
                model.keyboard = AppModel.KeyboardRequest(title: "Type", text: "")
            }
        }
    }

    private var transport: some View {
        HStack(spacing: 12) {
            RemoteButton(symbol: "gobackward.10", label: "Back 10 seconds") { model.press(.rewind) }
            RemoteButton(symbol: model.nowPlaying?.paused == false ? "pause.fill" : "playpause.fill", label: "Play or pause", prominent: true) {
                model.press(.playPause)
            }
            RemoteButton(symbol: "goforward.10", label: "Forward 10 seconds") { model.press(.forward) }
            VStack(spacing: 8) {
                RemoteButton(symbol: "speaker.plus", label: "Volume up", compact: true) { model.volume(delta: 5) }
                RemoteButton(symbol: "speaker.minus", label: "Volume down", compact: true) { model.volume(delta: -5) }
            }
        }
    }

    private var dotColor: Color {
        if !model.hasHost { return model.localNetworkDenied ? Brand.bad : Brand.warn }
        return switch model.kodiState {
        case .ready: Brand.ok
        case .connecting: Brand.warn
        default: Brand.bad
        }
    }

    private var connectionLabel: String {
        if !model.hasHost {
            return model.localNetworkDenied ? "Local Network access is off" : "Looking for your box…"
        }
        if model.kodiState != .ready, model.status?.kodi.running == false {
            return "TV app is starting… · \(model.host)"
        }
        return switch model.kodiState {
        case .ready: model.host
        case .connecting: "Connecting to \(model.host)…"
        case .failed: "Can't reach \(model.host)"
        case .idle: "Offline"
        }
    }
}

/// Apple TV-style surface.
/// - Drag: one step every `step` points; keep dragging to keep moving.
/// - Fast flick: momentum, extra steps that slow down (any touch stops it).
/// - Tap: select. Hold: context menu.
struct Touchpad: View {
    @Environment(AppModel.self) private var model
    @State private var anchor: CGPoint?
    @State private var lastKey: Key?
    @State private var travelled: CGFloat = 0
    @State private var began = Date()
    @State private var held = false
    @State private var hold: Task<Void, Never>?

    private let step: CGFloat = 34

    var body: some View {
        RoundedRectangle(cornerRadius: Brand.Radius.large, style: .continuous)
            .fill(Brand.panel)
            .overlay {
                RoundedRectangle(cornerRadius: Brand.Radius.large, style: .continuous)
                    .strokeBorder(Brand.line, lineWidth: 1)
            }
            .overlay {
                VStack(spacing: 6) {
                    Image(systemName: "hand.draw").font(.largeTitle).foregroundStyle(Brand.faint)
                    Text("Swipe to move · tap to select · hold for menu").font(.caption).foregroundStyle(Brand.muted)
                }
            }
            .contentShape(Rectangle())
            .gesture(drag)
            .accessibilityElement()
            .accessibilityLabel("Touchpad")
            .accessibilityHint("Swipe to move, tap to select")
            .accessibilityAction(named: "Select") { model.press(.select) }
            .accessibilityAction(named: "Up") { model.press(.up) }
            .accessibilityAction(named: "Down") { model.press(.down) }
            .accessibilityAction(named: "Left") { model.press(.left) }
            .accessibilityAction(named: "Right") { model.press(.right) }
    }

    private var drag: some Gesture {
        DragGesture(minimumDistance: 0, coordinateSpace: .local)
            .onChanged { value in
                if anchor == nil { touchDown(at: value.startLocation) }
                guard let start = anchor else { return }
                let dx = value.location.x - start.x
                let dy = value.location.y - start.y
                travelled = max(travelled, hypot(value.translation.width, value.translation.height))
                if travelled >= 10 { hold?.cancel() }
                guard abs(dx) >= step || abs(dy) >= step else { return }
                let key: Key = abs(dx) > abs(dy) ? (dx > 0 ? .right : .left) : (dy > 0 ? .down : .up)
                model.press(key)
                lastKey = key
                anchor = value.location
            }
            .onEnded { value in
                hold?.cancel()
                defer { anchor = nil; lastKey = nil }
                if travelled < 10 {
                    if !held, Date().timeIntervalSince(began) < 0.45 { model.press(.select) }
                    return
                }
                // Momentum along the last direction moved.
                guard let key = lastKey else { return }
                let v = value.velocity
                let speed = (key == .left || key == .right) ? abs(v.width) : abs(v.height)
                model.coast(key, steps: min(14, Int(speed / 550)))
            }
    }

    private func touchDown(at point: CGPoint) {
        model.cancelCoast()
        anchor = point
        travelled = 0
        began = Date()
        held = false
        hold = Task {
            try? await Task.sleep(for: .milliseconds(450))
            guard !Task.isCancelled, travelled < 10 else { return }
            held = true
            model.press(.menu)
        }
    }
}

struct RemoteButton: View {
    let symbol: String
    let label: String
    var prominent = false
    var compact = false
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            Image(systemName: symbol)
                .font(compact ? .body : .title2)
                .frame(maxWidth: .infinity, minHeight: compact ? 26 : 60)
                .foregroundStyle(prominent ? Brand.onAmber : Brand.text)
                .background(
                    RoundedRectangle(cornerRadius: Brand.Radius.medium, style: .continuous)
                        .fill(prominent ? Brand.amber : Brand.raised)
                )
        }
        .buttonStyle(PressStyle())
        .accessibilityLabel(label)
    }
}

/// Instant visual feedback on touch-down.
struct PressStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .scaleEffect(configuration.isPressed ? 0.94 : 1)
            .opacity(configuration.isPressed ? 0.8 : 1)
    }
}
