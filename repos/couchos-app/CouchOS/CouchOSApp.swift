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
            .tint(.orange)
            .overlay(alignment: .bottom) {
                if let toast = model.toast {
                    Text(toast)
                        .font(.subheadline.weight(.medium))
                        .padding(.horizontal, 16).padding(.vertical, 10)
                        .background(.regularMaterial, in: Capsule())
                        .padding(.bottom, 64)
                        .transition(.move(edge: .bottom).combined(with: .opacity))
                        .allowsHitTesting(false)
                }
            }
            .animation(.snappy, value: model.toast)
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
            .environment(model)
            .preferredColorScheme(.dark)
        }
        .onChange(of: scenePhase, initial: true) { _, phase in
            // Hold the Kodi socket only while the app is on screen.
            if phase == .active { model.activate() } else if phase == .background { model.deactivate() }
        }
    }
}
