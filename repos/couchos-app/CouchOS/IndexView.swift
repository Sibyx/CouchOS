import SwiftUI

/// The Plex index and artwork cache on the TV box: what it holds, how
/// current it is, what it is doing now, and the controls. Per-entry controls
/// (a show, a season, one movie) are on the long-press menu of each entry.
struct IndexView: View {
    @Environment(AppModel.self) private var model
    @State private var catalog: CatalogStatus?
    @State private var error: String?
    @State private var confirmReset = false
    @State private var confirmClear = false

    var body: some View {
        Form {
            if let catalog {
                statusSection(catalog)
                librariesSection(catalog)
                contentsSection(catalog)
                artworkSection(catalog)
                maintenanceSection
            } else if let error {
                ContentUnavailableView("Can't reach the TV box", systemImage: "wifi.exclamationmark", description: Text(error))
            } else {
                ProgressView().frame(maxWidth: .infinity)
            }
        }
        .navigationTitle("Library index")
        .navigationBarTitleDisplayMode(.inline)
        .refreshable { await load() }
        .task {
            // Live while open: every second during a sync, else every 5 s.
            while !Task.isCancelled {
                await load()
                let busy = catalog.map { !$0.jobs.isEmpty || $0.queued > 0 } ?? false
                try? await Task.sleep(for: .seconds(busy ? 1 : 5))
            }
        }
        .confirmationDialog("Rebuild the index?", isPresented: $confirmReset, titleVisibility: .visible) {
            Button("Rebuild", role: .destructive) { act("Rebuilding the index") { try await $0.catalogReset() } }
        } message: {
            Text("Everything is read from Plex again. Browsing falls back to the server until it's done.")
        }
        .confirmationDialog("Clear saved artwork?", isPresented: $confirmClear, titleVisibility: .visible) {
            Button("Clear", role: .destructive) { act("Clearing artwork") { try await $0.catalogClearArt() } }
        } message: {
            Text("Posters are fetched again from Plex as you browse.")
        }
    }

    // MARK: sections

    private func statusSection(_ catalog: CatalogStatus) -> some View {
        Section {
            VStack(alignment: .leading, spacing: 6) {
                HStack(spacing: 10) {
                    statusIcon(catalog)
                    VStack(alignment: .leading, spacing: 2) {
                        Text(headline(catalog)).font(.headline)
                        Text(detail(catalog)).font(.caption).foregroundStyle(.secondary)
                    }
                }
                ForEach(catalog.jobs) { job in
                    VStack(alignment: .leading, spacing: 3) {
                        Text(job.label).font(.caption).lineLimit(2)
                        if let fraction = job.fraction {
                            ProgressView(value: fraction).tint(.orange)
                            Text("\(job.done.formatted()) of \(job.total.formatted())")
                                .font(.caption2).foregroundStyle(.secondary).monospacedDigit()
                        } else {
                            ProgressView().controlSize(.small)
                        }
                    }
                    .padding(.top, 4)
                }
                if catalog.queued > 0 {
                    Text("\(catalog.queued) more queued").font(.caption2).foregroundStyle(.secondary)
                }
                if let error = catalog.error, catalog.jobs.isEmpty {
                    Text(error).font(.caption).foregroundStyle(.red)
                }
            }
            .padding(.vertical, 4)
            HStack {
                Button("Check for changes") { act("Checking Plex for changes") { try await $0.catalogSync(full: false) } }
                Spacer()
                Button("Full sync") { act("Full sync queued") { try await $0.catalogSync(full: true) } }
            }
            .buttonStyle(.borderless)
        } footer: {
            Text("Changes are checked every \(minutes(catalog.intervals.quick)); everything is re-read every \(hours(catalog.intervals.full)).")
        }
    }

    private func librariesSection(_ catalog: CatalogStatus) -> some View {
        Section {
            ForEach(catalog.sections) { section in
                LibraryRow(section: section) { message, work in act(message, work) }
            }
        } header: {
            Text("Libraries")
        } footer: {
            Text("Off: left out of the index and search. Keep artwork: posters are saved ahead of time, so grids load instantly.")
        }
    }

    private func contentsSection(_ catalog: CatalogStatus) -> some View {
        Section("Contents") {
            LabeledContent("Movies", value: (catalog.counts["movie"] ?? 0).formatted())
            LabeledContent("Shows", value: (catalog.counts["show"] ?? 0).formatted())
            LabeledContent("Seasons", value: (catalog.counts["season"] ?? 0).formatted())
            LabeledContent("Episodes", value: (catalog.counts["episode"] ?? 0).formatted())
            LabeledContent("Index size", value: Int64(catalog.dbBytes).formatted(.byteCount(style: .file)))
        }
        .monospacedDigit()
    }

    private func artworkSection(_ catalog: CatalogStatus) -> some View {
        Section {
            VStack(alignment: .leading, spacing: 6) {
                Gauge(value: Double(catalog.art.bytes), in: 0...Double(max(catalog.art.limitBytes, 1))) {
                    EmptyView()
                }
                .tint(.orange)
                HStack {
                    Text("\(Int64(catalog.art.bytes).formatted(.byteCount(style: .file))) of \(Int64(catalog.art.limitBytes).formatted(.byteCount(style: .file)))")
                    Spacer()
                    Text("\(catalog.art.files.formatted()) pictures")
                }
                .font(.caption).foregroundStyle(.secondary).monospacedDigit()
            }
            Button("Clear saved artwork", role: .destructive) { confirmClear = true }
        } header: {
            Text("Artwork on the TV box")
        } footer: {
            Text("When full, the pictures not looked at for the longest are dropped.")
        }
    }

    private var maintenanceSection: some View {
        Section {
            Button("Rebuild the index", role: .destructive) { confirmReset = true }
        } footer: {
            if let server = catalog?.server { Text("Plex server: \(server)") }
        }
    }

    // MARK: helpers

    @ViewBuilder private func statusIcon(_ catalog: CatalogStatus) -> some View {
        switch catalog.state {
        case "syncing":
            Image(systemName: "arrow.triangle.2.circlepath").foregroundStyle(.orange)
                .symbolEffect(.rotate, options: .repeat(.continuous))
        case "error":
            Image(systemName: "exclamationmark.triangle.fill").foregroundStyle(.yellow)
        case "empty":
            Image(systemName: "hourglass").foregroundStyle(.secondary)
        default:
            Image(systemName: "checkmark.circle.fill").foregroundStyle(.green)
        }
    }

    private func headline(_ catalog: CatalogStatus) -> String {
        switch catalog.state {
        case "syncing": "Syncing with Plex"
        case "error": "Last sync failed"
        case "empty": "Not indexed yet"
        default: "Up to date"
        }
    }

    private func detail(_ catalog: CatalogStatus) -> String {
        var parts: [String] = []
        if let synced = catalog.syncedAt {
            parts.append("Checked \(relative(synced))")
        }
        if let full = catalog.fullSyncedAt {
            parts.append("full sync \(relative(full))")
        }
        return parts.isEmpty ? "The first sync starts shortly after couchd starts." : parts.joined(separator: " · ")
    }

    private func load() async {
        guard let couchd = model.couchd else { return }
        do {
            catalog = try await couchd.catalogStatus()
            error = nil
        } catch {
            if catalog == nil { self.error = error.localizedDescription }
        }
    }

    private func act(_ message: String, _ work: @escaping (Couchd) async throws -> Void) {
        model.show(toast: message)
        model.run { api in
            try await work(api)
            await load()
        }
    }

    private func minutes(_ seconds: Double) -> String {
        Duration.seconds(seconds).formatted(.units(allowed: [.minutes], width: .wide))
    }

    private func hours(_ seconds: Double) -> String {
        Duration.seconds(seconds).formatted(.units(allowed: [.hours], width: .wide))
    }
}

func relative(_ timestamp: Double) -> String {
    Date(timeIntervalSince1970: timestamp).formatted(.relative(presentation: .named))
}

/// One library: in the index or not, keep artwork, its state, and actions.
private struct LibraryRow: View {
    let section: CatalogStatus.Section
    let act: (String, @escaping (Couchd) async throws -> Void) -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Toggle(isOn: Binding(get: { section.isEnabled }, set: { on in
                act(on ? "Adding \(section.title) to the index" : "Removing \(section.title) from the index") {
                    try await $0.catalogSection(section.key, enabled: on)
                }
            })) {
                VStack(alignment: .leading, spacing: 2) {
                    Label(section.title, systemImage: section.symbol)
                    Text(state).font(.caption).foregroundStyle(section.status == "error" ? .red : .secondary)
                }
            }
            if section.isEnabled {
                Toggle("Keep artwork", isOn: Binding(get: { section.keepsArt }, set: { on in
                    act(on ? "Saving artwork for \(section.title)" : "Artwork for \(section.title) is saved as you browse") {
                        try await $0.catalogSection(section.key, keepArt: on)
                    }
                }))
                .font(.subheadline)
            }
        }
        .swipeActions(edge: .trailing) {
            if section.isEnabled {
                Button("Full sync") {
                    act("Full sync of \(section.title)") { try await $0.catalogSync(section: section.key, full: true) }
                }
                .tint(.orange)
                Button("Artwork") {
                    act("Saving artwork for \(section.title)") { try await $0.catalogArt(section: section.key, fetch: true) }
                }
                .tint(.blue)
            }
        }
        .contextMenu {
            if section.isEnabled {
                Button { act("Checking \(section.title) for changes") { try await $0.catalogSync(section: section.key, full: false) } } label: {
                    Label("Check for changes", systemImage: "arrow.clockwise")
                }
                Button { act("Full sync of \(section.title)") { try await $0.catalogSync(section: section.key, full: true) } } label: {
                    Label("Full sync", systemImage: "arrow.triangle.2.circlepath")
                }
                Button { act("Saving artwork for \(section.title)") { try await $0.catalogArt(section: section.key, fetch: true) } } label: {
                    Label("Save all artwork", systemImage: "square.and.arrow.down")
                }
                Button(role: .destructive) {
                    act("Removing artwork of \(section.title)") { try await $0.catalogArt(section: section.key, fetch: false) }
                } label: {
                    Label("Remove its artwork", systemImage: "trash")
                }
            }
        }
    }

    private var state: String {
        guard section.isEnabled else { return "Not indexed" }
        if section.status == "syncing" { return "Syncing…" }
        if section.status == "error" { return section.error ?? "Sync failed" }
        let count = section.contents
        guard let full = section.fullSyncedAt else { return "Waiting for the first sync" }
        let checked = section.syncedAt.map { " · checked \(relative($0))" } ?? ""
        return "\(count) · full sync \(relative(full))\(checked)"
    }
}
