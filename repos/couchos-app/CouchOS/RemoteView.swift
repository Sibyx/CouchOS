import SwiftUI

struct RemoteView: View {
    @Environment(AppModel.self) private var model
    @State private var showMore = false

    var body: some View {
        @Bindable var model = model
        VStack(spacing: 16) {
            header
            nowPlaying
            Touchpad()
            controls
            transport
        }
        .padding(.horizontal, 16)
        .padding(.bottom, 8)
        .background(Color.black.ignoresSafeArea())
        .sheet(isPresented: $showMore) { MoreView() }
        .sheet(item: $model.keyboard) { request in KeyboardSheet(request: request) }
        .alert("CouchOS", isPresented: Binding(get: { model.message != nil }, set: { if !$0 { model.message = nil } })) {
            Button("OK", role: .cancel) {}
        } message: {
            Text(model.message ?? "")
        }
    }

    private var header: some View {
        HStack {
            Circle().fill(dotColor).frame(width: 8, height: 8)
            Text(connectionLabel).font(.footnote).foregroundStyle(.secondary)
            Spacer()
            Picker("Touchpad", selection: Bindable(model).pointerMode) {
                Image(systemName: "arrow.up.and.down.and.arrow.left.and.right").tag(false)
                    .accessibilityLabel("Swipe navigation")
                Image(systemName: "cursorarrow").tag(true)
                    .accessibilityLabel("Mouse pointer")
            }
            .pickerStyle(.segmented)
            .frame(width: 110)
            Button { showMore = true } label: {
                Image(systemName: "slider.horizontal.3").font(.title3)
            }
            .accessibilityLabel("Screen, sound and Bluetooth")
        }
        .padding(.top, 8)
    }

    private var nowPlaying: some View {
        HStack {
            VStack(alignment: .leading, spacing: 2) {
                Text(model.nowPlaying?.title ?? modeTitle)
                    .font(.headline).lineLimit(1)
                Text(model.nowPlaying?.subtitle ?? " ")
                    .font(.subheadline).foregroundStyle(.secondary).lineLimit(1)
            }
            Spacer()
        }
        .frame(minHeight: 44)
    }

    private var controls: some View {
        HStack(spacing: 12) {
            RemoteButton(symbol: "arrow.uturn.backward", label: "Back") { model.press(.back) }
            RemoteButton(symbol: "house", label: "Home") { model.press(.home) }
            RemoteButton(symbol: "info.circle", label: "Info") { model.press(.info) }
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
        switch model.kodiState {
        case .ready: .green
        case .connecting: .yellow
        default: .red
        }
    }

    private var connectionLabel: String {
        if model.kodiState != .ready, model.status?.kodi.running == false {
            return "Kodi off · \(model.host)"
        }
        return switch model.kodiState {
        case .ready: model.host
        case .connecting: "Connecting to \(model.host)…"
        case .failed: "Can't reach \(model.host)"
        case .idle: "Offline"
        }
    }

    private var modeTitle: String {
        switch model.status?.mode {
        case "airplay": model.status?.airplay.connected == true ? "AirPlay mirroring" : "Waiting for AirPlay"
        case "idle": "Screen off"
        default: "Plex"
        }
    }
}

/// Apple TV-style surface: swipe = one step per `step` points (drag on to
/// scroll), tap = select, long press = context menu.
struct Touchpad: View {
    @Environment(AppModel.self) private var model
    @State private var anchor: CGPoint?
    @State private var travelled: CGFloat = 0
    @State private var began = Date()
    @State private var longPressFired = false
    @State private var longPress: Task<Void, Never>?

    private let step: CGFloat = 36

    var body: some View {
        RoundedRectangle(cornerRadius: 28, style: .continuous)
            .fill(Color(white: 0.11))
            .overlay {
                Image(systemName: model.pointerMode ? "cursorarrow.motionlines" : "hand.point.up.left")
                    .font(.largeTitle).foregroundStyle(Color(white: 0.25))
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
                if anchor == nil {
                    anchor = value.startLocation
                    travelled = 0
                    began = Date()
                    longPressFired = false
                    longPress = Task {
                        try? await Task.sleep(for: .milliseconds(450))
                        guard !Task.isCancelled, travelled < 10 else { return }
                        longPressFired = true
                        model.press(.menu)
                    }
                }
                guard let start = anchor else { return }
                let dx = value.location.x - start.x
                let dy = value.location.y - start.y
                travelled = max(travelled, hypot(value.translation.width, value.translation.height))
                if model.pointerMode {
                    if travelled >= 4 { longPress?.cancel() }
                    model.movePointer(dx: dx, dy: dy)
                    anchor = value.location
                } else if abs(dx) >= step || abs(dy) >= step {
                    longPress?.cancel()
                    if abs(dx) > abs(dy) {
                        model.press(dx > 0 ? .right : .left)
                    } else {
                        model.press(dy > 0 ? .down : .up)
                    }
                    anchor = value.location
                }
            }
            .onEnded { _ in
                longPress?.cancel()
                if travelled < 10, !longPressFired, Date().timeIntervalSince(began) < 0.45 {
                    model.press(.select)
                }
                anchor = nil
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
                .foregroundStyle(prominent ? Color.black : Color.white)
                .background(
                    RoundedRectangle(cornerRadius: 16, style: .continuous)
                        .fill(prominent ? Color.orange : Color(white: 0.15))
                )
        }
        .buttonStyle(PressStyle())
        .accessibilityLabel(label)
    }
}

/// No SwiftUI tap delay: the action fires on touch-up with instant visual feedback.
struct PressStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .scaleEffect(configuration.isPressed ? 0.94 : 1)
            .opacity(configuration.isPressed ? 0.8 : 1)
    }
}
