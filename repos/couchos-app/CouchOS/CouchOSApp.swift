import SwiftUI

@main
struct CouchOSApp: App {
    @State private var model = AppModel()
    @Environment(\.scenePhase) private var scenePhase

    init() {
        // Posters come from couchd with a day's Cache-Control; keep them on
        // the phone too, so scrolling back never asks again.
        URLCache.shared = URLCache(memoryCapacity: 48 << 20, diskCapacity: 300 << 20)
    }

    var body: some Scene {
        WindowGroup {
            @Bindable var model = model
            TabView(selection: $model.tab) {
                Tab("Browse", systemImage: "square.stack", value: AppTab.browse) { BrowseView() }
                Tab("Remote", systemImage: "appletvremote.gen4", value: AppTab.remote) { RemoteView() }
            }
            .tint(Brand.amber)
            .overlay(alignment: .bottom) {
                if let toast = model.toast {
                    Text(toast)
                        .font(.subheadline.weight(.medium))
                        .foregroundStyle(Brand.text)
                        .padding(.horizontal, 16).padding(.vertical, 10)
                        .background(.regularMaterial, in: Capsule())
                        .overlay(Capsule().strokeBorder(Brand.line))
                        .padding(.bottom, 64)
                        .transition(.move(edge: .bottom).combined(with: .opacity))
                        .allowsHitTesting(false)
                }
            }
            .animation(.snappy, value: model.toast)
            // On the root, so a failure on any tab (Browse, the index, Remote) is seen.
            .errorAlert(model)
            .confirmationDialog("The TV is showing AirPlay", isPresented: Binding(
                get: { model.playOverAirPlay != nil }, set: { if !$0 { model.playOverAirPlay = nil } }
            ), titleVisibility: .visible, presenting: model.playOverAirPlay) { pending in
                Button("Play \(pending.item.title) anyway") {
                    model.playOnTV(pending.item, fromStart: pending.fromStart, replace: true)
                }
                Button("Cancel", role: .cancel) {}
            } message: { _ in
                Text("Playing it ends the screen sharing on the phone that's using AirPlay.")
            }
            .sheet(isPresented: $model.showSettings) { MoreView() }
            .environment(model)
            .preferredColorScheme(.dark)
        }
        .onChange(of: scenePhase, initial: true) { _, phase in
            // Hold the Kodi socket only while the app is on screen.
            if phase == .active { model.activate() } else if phase == .background { model.deactivate() }
        }
    }
}

extension View {
    /// Shows `model.message` (a failed action) as an alert. On the root and on
    /// the settings sheet too: a view presenting a sheet can't show an alert.
    func errorAlert(_ model: AppModel) -> some View {
        alert("Something went wrong", isPresented: Binding(
            get: { model.message != nil }, set: { if !$0 { model.message = nil } }
        )) {
            Button("OK", role: .cancel) {}
        } message: {
            Text(model.message ?? "")
        }
    }
}
