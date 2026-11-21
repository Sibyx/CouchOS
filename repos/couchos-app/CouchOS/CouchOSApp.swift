import SwiftUI

@main
struct CouchOSApp: App {
    @State private var model = AppModel()
    @Environment(\.scenePhase) private var scenePhase

    var body: some Scene {
        WindowGroup {
            RemoteView()
                .environment(model)
                .preferredColorScheme(.dark)
        }
        .onChange(of: scenePhase, initial: true) { _, phase in
            // Hold the Kodi socket only while the app is on screen.
            if phase == .active { model.activate() } else if phase == .background { model.deactivate() }
        }
    }
}
