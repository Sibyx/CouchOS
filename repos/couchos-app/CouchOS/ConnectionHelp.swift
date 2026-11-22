import SwiftUI
import UIKit

/// Shown instead of content while there is no TV box to talk to: Local
/// Network access is off, or Bonjour hasn't found a box yet. Says what to
/// check and offers the one action that fixes it.
struct ConnectionHelp: View {
    @Environment(AppModel.self) private var model
    @Environment(\.openURL) private var openURL

    var body: some View {
        ContentUnavailableView {
            Label(title, systemImage: symbol)
        } description: {
            Text(detail)
        } actions: {
            if model.localNetworkDenied {
                Button("Open Settings") {
                    if let url = URL(string: UIApplication.openSettingsURLString) { openURL(url) }
                }
                .buttonStyle(.borderedProminent)
                .foregroundStyle(Brand.onAmber)
            } else {
                ProgressView().padding(.bottom, 4)
                Button("Enter the address instead") { model.showSettings = true }
            }
        }
    }

    private var title: String {
        model.localNetworkDenied ? "Allow Local Network access" : "Looking for your CouchOS box"
    }

    private var symbol: String {
        model.localNetworkDenied ? "network.slash" : "tv.badge.wifi"
    }

    private var detail: String {
        model.localNetworkDenied
            ? "CouchOS finds and controls your TV box over your home Wi-Fi. Turn on Local Network for CouchOS in Settings."
            : "Make sure this iPhone is on the same Wi-Fi as the box, and the box is on. It's found automatically."
    }
}
