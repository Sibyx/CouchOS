import SwiftUI

/// Control plane (couchd): TV power, sound, Bluetooth, host.
/// There is no "mode": the TV is always Plex, AirPlay takes over by itself.
struct MoreView: View {
    @Environment(AppModel.self) private var model
    @Environment(\.dismiss) private var dismiss
    @State private var hostField = ""

    var body: some View {
        NavigationStack {
            Form {
                Section("TV") {
                    HStack {
                        Button("Turn on") { model.tv("on") }
                        Spacer()
                        Button("Standby") { model.tv("standby") }
                    }
                    .buttonStyle(.borderless)
                    if model.airplayActive {
                        Button("Stop AirPlay", role: .destructive) { model.stopAirPlay() }
                    }
                }

                Section {
                    HStack {
                        Button { model.volume(delta: -5) } label: { Image(systemName: "speaker.minus") }
                        Spacer()
                        Text(volumeText).monospacedDigit()
                        Spacer()
                        Button { model.volume(delta: 5) } label: { Image(systemName: "speaker.plus") }
                    }
                    .buttonStyle(.borderless)
                    Toggle("Mute", isOn: Binding(get: { model.audio?.muted ?? false }, set: { _ in model.toggleMute() }))
                } header: {
                    Text("Sound")
                } footer: {
                    Text(model.audio?.sink ?? "")
                }

                Section("Bluetooth speakers") {
                    ForEach(speakers) { device in
                        HStack {
                            VStack(alignment: .leading) {
                                Text(device.name)
                                if device.connected { Text("Connected").font(.caption).foregroundStyle(.green) }
                            }
                            Spacer()
                            if !device.paired {
                                Button("Pair") { model.bluetooth("pair", device) }
                            } else if device.connected {
                                Button("Disconnect") { model.bluetooth("disconnect", device) }
                            } else {
                                Button("Connect") { model.bluetooth("connect", device) }
                            }
                        }
                        .buttonStyle(.borderless)
                        .swipeActions {
                            if device.paired {
                                Button("Forget", role: .destructive) { model.bluetooth("remove", device) }
                            }
                        }
                    }
                    Button(model.bluetooth?.scanning == true ? "Scanning…" : "Scan for speakers") { model.scanBluetooth() }
                        .disabled(model.bluetooth?.scanning == true)
                }

                Section {
                    TextField("Found automatically", text: $hostField)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                        .keyboardType(.URL)
                        .onSubmit { model.pinHost(hostField) }
                    Button("Restart Kodi", role: .destructive) { model.restartKodi() }
                } header: {
                    Text("Device")
                } footer: {
                    VStack(alignment: .leading, spacing: 4) {
                        Text(model.hostPinned ? "Using this host. Clear it to find the TV box automatically."
                                              : "Found automatically: \(model.host). Type a host to use another.")
                        if let system = model.status?.system {
                            Text("\(system.tempC.map { String(format: "%.0f °C", $0) } ?? "–") · \(system.memAvailableMb) MB free")
                        }
                    }
                }
            }
            .navigationTitle("CouchOS")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar { ToolbarItem(placement: .confirmationAction) { Button("Done") { dismiss() } } }
            .task {
                hostField = model.hostPinned ? model.host : ""
                await model.refreshBluetooth()
                await model.refreshStatus()
            }
        }
    }

    private var speakers: [BluetoothState.Device] {
        (model.bluetooth?.devices ?? []).filter { $0.audio || $0.paired }
    }

    private var volumeText: String {
        guard let audio = model.audio, let volume = audio.volume else { return "–" }
        return audio.muted ? "Muted" : "\(volume) %"
    }
}
