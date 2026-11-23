import SwiftUI

/// Dashboard → Movies to watch: a shared Reminders list, one film per
/// reminder, shown on the TV's dashboard when the Plex library has it. Each
/// entry says whether the box found it, so a link that doesn't match shows.
struct MovieListSection: View {
    @Environment(AppModel.self) private var model
    @State private var listID: String?
    @State private var lists: [ReminderStore.List] = []
    @State private var watchlist: Watchlist?
    @State private var busy = false

    /// A list with one of these names is picked first.
    private static let likelyNames = ["movies", "films", "watchlist", "to watch", "filmy"]

    var body: some View {
        Section {
            if listID == nil {
                Button { Task { await start() } } label: {
                    Label("Show a movies list on the TV", systemImage: "film.stack")
                }
                .disabled(!model.hasHost || busy)
                // Films left on the TV (Reminders access was turned off):
                // taking them down needs no access.
                if (watchlist?.items.isEmpty == false) {
                    Button("Remove the movies from the TV", role: .destructive) { choose(nil) }
                        .disabled(busy)
                }
            } else {
                Picker("List", selection: Binding(get: { listID ?? "" }, set: { choose($0) })) {
                    ForEach(lists) { Text($0.title).tag($0.id) }
                }
                ForEach(watchlist?.items ?? []) { entry in
                    row(entry)
                        .swipeActions {
                            Button("Watched") { work { try await $0.setDone(entry.id) } }
                                .tint(Brand.ok)
                        }
                }
                Button("Sync now") { choose(listID) }
                    .disabled(busy)
                Button("Stop showing movies", role: .destructive) { choose(nil) }
                    .disabled(busy)
            }
        } header: {
            Text("Movies to watch")
        } footer: {
            Text(footer)
        }
        .task { await load() }
    }

    private var footer: String {
        let format = "One film per reminder: its name as the title, and a link in the notes (IMDb, TMDB or Letterboxd). Only films in your Plex library show on the TV."
        guard let watchlist, listID != nil else { return format }
        return "\(watchlist.shown) of \(watchlist.items.count) on the TV. Swipe for Watched. \(format)"
    }

    private func row(_ entry: Watchlist.Entry) -> some View {
        HStack(spacing: 12) {
            VStack(alignment: .leading, spacing: 2) {
                Text(entry.title.isEmpty ? "Untitled" : entry.title)
                    .foregroundStyle(Brand.text)
                if let match = entry.matched, let title = match.title {
                    Text([title, match.year.map(String.init)].compactMap { $0 }.joined(separator: " · "))
                        .font(.footnote)
                        .foregroundStyle(Brand.muted)
                } else if let year = entry.year {
                    Text(String(year)).font(.footnote).foregroundStyle(Brand.muted)
                }
            }
            Spacer()
            if entry.available {
                Label("In Plex", systemImage: "checkmark.circle.fill")
                    .labelStyle(.iconOnly)
                    .foregroundStyle(Brand.ok)
            } else {
                Text("Not in Plex").font(.footnote).foregroundStyle(Brand.muted)
            }
        }
        .accessibilityElement(children: .combine)
        .accessibilityValue(entry.available ? "In Plex, on the TV" : "Not in Plex")
    }

    private func load() async {
        let sync = MovieListSync.shared
        listID = sync.isOn ? sync.listID : nil
        lists = ReminderStore.shared.lists()
        guard model.hasHost else { return }
        // Synced first, so the list opens in step with Reminders.
        if sync.isOn, let synced = try? await sync.sync() {
            watchlist = synced
        } else {
            watchlist = try? await sync.status()
        }
    }

    private func start() async {
        guard await ReminderStore.shared.requestAccess() else {
            model.message = ReminderError.noAccess.localizedDescription
            return
        }
        lists = ReminderStore.shared.lists()
        let likely = lists.first { Self.likelyNames.contains($0.title.lowercased()) } ?? lists.first
        guard let likely else {
            model.message = "There are no lists in Reminders yet. Make one (e.g. “Movies”) and share it."
            return
        }
        choose(likely.id)
    }

    private func choose(_ list: String?) {
        listID = list
        work { try await $0.choose(list) }
        if list != nil { ReminderStore.shared.observe() }
    }

    private func work(_ job: @escaping @MainActor (MovieListSync) async throws -> Watchlist?) {
        busy = true
        Task {
            defer { busy = false }
            do {
                if let updated = try await job(MovieListSync.shared) { watchlist = updated }
            } catch {
                model.message = error.localizedDescription
            }
        }
    }
}
