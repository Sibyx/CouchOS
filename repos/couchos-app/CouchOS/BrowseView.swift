import SwiftUI

/// The Plex catalog: Continue Watching and the other home rows, libraries,
/// and search, all served from the index on the Pi. Picking a movie or
/// episode plays it on the TV. Long-press anything for its index and artwork
/// controls; the drive button opens the index itself.
struct BrowseView: View {
    @Environment(AppModel.self) private var model
    @State private var hubs: [PlexHub] = []
    @State private var sections: [PlexSection] = []
    @State private var error: String?
    @State private var loaded = false
    @State private var query = ""
    @State private var scope: SearchScope = .all
    @State private var results: PlexSearch?
    @State private var searching = false
    @AppStorage("recentSearches") private var recentStore = ""

    var body: some View {
        NavigationStack {
            ScrollView {
                if !model.hasHost {
                    ConnectionHelp().containerRelativeFrame(.vertical)
                } else if !query.trimmingCharacters(in: .whitespaces).isEmpty {
                    SearchResults(results: results, query: query, searching: searching) { remember(query) }
                } else if isSearchFocused {
                    RecentSearches(recents: recents) { query = $0 } clear: { recentStore = "" }
                } else {
                    home
                }
            }
            .scrollDismissesKeyboard(.immediately)
            .background(Brand.ink.ignoresSafeArea())
            .navigationTitle("Plex")
            .navigationDestination(for: PlexItem.self) { ItemView(item: $0) }
            .navigationDestination(for: PlexSection.self) { SectionView(section: $0) }
            .navigationDestination(for: IndexRoute.self) { _ in IndexView() }
            .toolbar { if model.hasHost { ToolbarItem(placement: .topBarTrailing) { IndexButton() } } }
            .safeAreaInset(edge: .top, spacing: 0) { SyncBar() }
            .searchable(text: $query, isPresented: $searchPresented, prompt: "Movies, shows, episodes, cast")
            .searchScopes($scope, activation: .onSearchPresentation) {
                ForEach(SearchScope.allCases) { Text($0.label).tag($0) }
            }
            .onSubmit(of: .search) { remember(query) }
            .task(id: "\(query)|\(scope.rawValue)") { await search() }
            // Loads once per box: on first show, and when a box is found or changed.
            .task(id: model.host) { if loadedHost != model.host { await load() } }
            .refreshable { await load() }
            .overlay { if model.hasHost && !loaded && error == nil { ProgressView() } }
            .onChange(of: model.status?.catalog?.state) { old, new in
                // A sync just finished: the rows may have changed.
                if old == "syncing", new != "syncing" { Task { await load() } }
            }
        }
    }

    @State private var searchPresented = false
    /// The box the rows on screen came from.
    @State private var loadedHost: String?
    private var isSearchFocused: Bool { searchPresented }

    @ViewBuilder private var home: some View {
        LazyVStack(alignment: .leading, spacing: 24) {
            if let error {
                ContentUnavailableView {
                    Label("Can't load Plex", systemImage: "exclamationmark.triangle")
                } description: {
                    Text(error)
                } actions: {
                    Button("Try again") { Task { await load() } }
                        .buttonStyle(.borderedProminent)
                        .foregroundStyle(Brand.onAmber)
                    Button("Connection settings") { model.showSettings = true }
                }
            }
            ForEach(hubs) { hub in PosterRow(title: hub.title, items: hub.items) }
            if !sections.isEmpty {
                VStack(alignment: .leading, spacing: 0) {
                    Text("Libraries").font(.title3.bold()).padding(.horizontal, 16).padding(.bottom, 8)
                    ForEach(sections) { section in
                        NavigationLink(value: section) {
                            HStack {
                                Label(section.title, systemImage: section.symbol)
                                Spacer()
                                if let count = section.count {
                                    Text(count.formatted()).foregroundStyle(.secondary).monospacedDigit()
                                }
                                Image(systemName: "chevron.right").foregroundStyle(.tertiary)
                            }
                            .padding(.horizontal, 16).padding(.vertical, 12)
                            .contentShape(Rectangle())
                        }
                        .buttonStyle(.plain)
                        Divider().padding(.leading, 16)
                    }
                }
            }
        }
        .padding(.vertical, 8)
    }

    private var recents: [String] {
        recentStore.split(separator: "\n").map(String.init)
    }

    private func remember(_ text: String) {
        let text = text.trimmingCharacters(in: .whitespaces)
        guard text.count >= 2 else { return }
        recentStore = ([text] + recents.filter { $0.caseInsensitiveCompare(text) != .orderedSame })
            .prefix(8).joined(separator: "\n")
    }

    private func load() async {
        guard let couchd = model.couchd else { return }
        if loadedHost != model.host {
            // Another box: drop the old one's rows rather than mixing them.
            hubs = []
            sections = []
            loaded = false
            error = nil
        }
        loadedHost = model.host
        do {
            async let hubs = couchd.plexHubs()
            async let sections = couchd.plexSections()
            (self.hubs, self.sections) = try await (hubs, sections)
            error = nil
        } catch {
            if hubs.isEmpty { self.error = error.localizedDescription }
        }
        loaded = true
    }

    private func search() async {
        let text = query.trimmingCharacters(in: .whitespaces)
        guard !text.isEmpty else { results = nil; return }
        // The index answers in milliseconds: only a short debounce, cancelled
        // by the next keystroke.
        try? await Task.sleep(for: .milliseconds(120))
        guard !Task.isCancelled, let couchd = model.couchd else { return }
        searching = true
        defer { searching = false }
        if let found = try? await couchd.plexSearch(text, scope: scope), !Task.isCancelled {
            results = found
        }
    }
}

/// Navigation value for the index screen.
struct IndexRoute: Hashable {}

/// The toolbar button: shows the index's state at a glance, opens it.
private struct IndexButton: View {
    @Environment(AppModel.self) private var model

    var body: some View {
        NavigationLink(value: IndexRoute()) {
            switch model.status?.catalog?.state {
            case "syncing":
                Image(systemName: "arrow.triangle.2.circlepath").symbolEffect(.rotate, options: .repeat(.continuous))
            case "error":
                Image(systemName: "exclamationmark.arrow.triangle.2.circlepath").foregroundStyle(Brand.warn)
            default:
                Image(systemName: "externaldrive.badge.checkmark")
            }
        }
        .accessibilityLabel("Library index")
    }
}

/// A thin progress line while the Pi syncs, with what it is doing.
private struct SyncBar: View {
    @Environment(AppModel.self) private var model

    var body: some View {
        if let catalog = model.status?.catalog, catalog.state == "syncing" {
            VStack(spacing: 4) {
                if let fraction = catalog.fraction {
                    ProgressView(value: fraction).tint(Brand.amber)
                } else {
                    ProgressView(value: 0).tint(Brand.amber).opacity(0.4)
                }
                if let label = catalog.label {
                    Text(label).font(.caption2).foregroundStyle(.secondary).lineLimit(1)
                }
            }
            .padding(.horizontal, 16).padding(.bottom, 6)
            .background(Brand.ink)
            .transition(.opacity)
        }
    }
}

private struct RecentSearches: View {
    let recents: [String]
    let pick: (String) -> Void
    let clear: () -> Void

    var body: some View {
        if recents.isEmpty {
            ContentUnavailableView("Search Plex", systemImage: "magnifyingglass",
                                   description: Text("Titles, shows, episodes, actors, genres or a year. Accents are optional."))
        } else {
            VStack(alignment: .leading, spacing: 0) {
                HStack {
                    Text("Recent").font(.headline)
                    Spacer()
                    Button("Clear", action: clear).font(.subheadline)
                }
                .padding(.horizontal, 16).padding(.vertical, 10)
                ForEach(recents, id: \.self) { text in
                    Button { pick(text) } label: {
                        Label(text, systemImage: "clock.arrow.circlepath")
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .padding(.horizontal, 16).padding(.vertical, 10)
                            .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                    Divider().padding(.leading, 16)
                }
            }
        }
    }
}

private struct SearchResults: View {
    let results: PlexSearch?
    let query: String
    let searching: Bool
    let opened: () -> Void

    var body: some View {
        if let results {
            if results.groups.isEmpty {
                ContentUnavailableView.search(text: query)
            } else {
                LazyVStack(alignment: .leading, spacing: 0) {
                    if !results.indexed {
                        Label("The TV box is still indexing Plex: these come straight from the server.", systemImage: "hourglass")
                            .font(.caption).foregroundStyle(.secondary).padding(16)
                    }
                    ForEach(results.groups) { group in
                        Text(group.title).font(.headline).foregroundStyle(.secondary)
                            .padding(.horizontal, 16).padding(.top, 16).padding(.bottom, 4)
                        ForEach(group.items) { item in
                            ItemRow(item: item).simultaneousGesture(TapGesture().onEnded(opened))
                        }
                    }
                }
            }
        } else if searching {
            ProgressView().padding(40)
        }
    }
}

/// A titled, horizontally scrolling row of posters (a Plex home hub).
struct PosterRow: View {
    let title: String
    let items: [PlexItem]

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(title).font(.title3.bold()).padding(.horizontal, 16)
            ScrollView(.horizontal, showsIndicators: false) {
                LazyHStack(alignment: .top, spacing: 12) {
                    ForEach(items) { item in
                        NavigationLink(value: item) { PosterCard(item: item).frame(width: 112) }
                            .buttonStyle(.plain)
                            .contextMenu { ItemMenu(item: item) } preview: { ItemPreview(item: item) }
                    }
                }
                .padding(.horizontal, 16)
            }
        }
    }
}

struct PosterCard: View {
    let item: PlexItem

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            PlexArtwork(path: item.poster)
                .overlay(alignment: .bottom) {
                    if let progress = item.progress {
                        ProgressView(value: progress).tint(Brand.amber).padding(6)
                    }
                }
                .overlay(alignment: .topTrailing) {
                    if let unwatched = item.unwatchedCount, unwatched > 0 {
                        Text("\(unwatched)").font(.caption2.bold()).padding(.horizontal, 6).padding(.vertical, 2)
                            .background(Capsule().fill(Brand.amber)).foregroundStyle(Brand.onAmber).padding(5)
                    } else if item.watched == true {
                        Image(systemName: "checkmark.circle.fill").font(.caption)
                            .foregroundStyle(.white, .black.opacity(0.6)).padding(5)
                    }
                }
            // Episodes and seasons are named by their show first ("Scrubs",
            // then "Season 6"), the way they're thought of.
            let underShow = item.type == "episode" || item.type == "season"
            Text(underShow ? (item.show ?? item.title) : item.title)
                .font(.caption).lineLimit(1)
            Text(underShow ? item.title : item.subtitle)
                .font(.caption2).foregroundStyle(.secondary).lineLimit(1)
        }
    }
}

/// One line in a list: thumbnail, title, subtitle, and Play for playable items.
struct ItemRow: View {
    @Environment(AppModel.self) private var model
    let item: PlexItem

    var body: some View {
        NavigationLink(value: item) {
            HStack(spacing: 12) {
                PlexArtwork(path: item.type == "episode" ? item.thumb : item.poster,
                            aspect: item.type == "episode" ? 16.0 / 9.0 : 2.0 / 3.0, width: 200)
                    .frame(width: item.type == "episode" ? 96 : 44)
                VStack(alignment: .leading, spacing: 2) {
                    Text(item.title).lineLimit(1)
                    Text(item.subtitle).font(.caption).foregroundStyle(.secondary).lineLimit(1)
                    if let progress = item.progress {
                        ProgressView(value: progress).tint(Brand.amber).frame(maxWidth: 120)
                    }
                }
                Spacer(minLength: 0)
                if item.watched == true { Image(systemName: "checkmark.circle.fill").foregroundStyle(.secondary) }
                if item.isPlayable {
                    Button { model.playOnTV(item) } label: {
                        Image(systemName: "play.circle.fill").font(.title).foregroundStyle(Brand.amber)
                    }
                    .buttonStyle(.borderless)
                    .accessibilityLabel("Play \(item.title) on TV")
                }
            }
            .padding(.horizontal, 16).padding(.vertical, 6)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .contextMenu { ItemMenu(item: item) }
    }
}

/// Long-press menu for any entry: play, and its index and artwork controls.
/// For a show or season they cover everything under it.
struct ItemMenu: View {
    @Environment(AppModel.self) private var model
    let item: PlexItem

    var body: some View {
        if item.isPlayable {
            Button { model.playOnTV(item) } label: { Label("Play on TV", systemImage: "play.fill") }
            if item.inProgress {
                Button { model.playOnTV(item, fromStart: true) } label: {
                    Label("Play from the start", systemImage: "backward.end.fill")
                }
            }
        } else {
            Button { model.playOnTV(item) } label: { Label("Open on TV", systemImage: "tv") }
        }
        Section(scopeTitle) {
            Button { model.catalog(.refresh, item) } label: {
                Label("Refresh from Plex", systemImage: "arrow.clockwise")
            }
            Button { model.catalog(.fetchArt, item) } label: {
                Label("Save artwork on the TV box", systemImage: "square.and.arrow.down")
            }
            Button(role: .destructive) { model.catalog(.dropArt, item) } label: {
                Label("Remove saved artwork", systemImage: "trash")
            }
        }
    }

    private var scopeTitle: String {
        switch item.type {
        case "show": "This show, its seasons and episodes"
        case "season": "This season and its episodes"
        default: "Index"
        }
    }
}

private struct ItemPreview: View {
    let item: PlexItem

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            PlexArtwork(path: item.poster, width: 400).frame(width: 200)
            Text(item.title).font(.headline)
            if !item.subtitle.isEmpty { Text(item.subtitle).font(.subheadline).foregroundStyle(.secondary) }
        }
        .padding(12)
        .background(Brand.panel)
    }
}

/// A movie, episode, show or season: details, Play on TV, its children, and
/// how current the TV box's copy is (pull down to refresh it from Plex).
struct ItemView: View {
    @Environment(AppModel.self) private var model
    let item: PlexItem
    @State private var full: PlexItem?
    @State private var error: String?

    private var shown: PlexItem { full ?? item }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                header
                actions
                if let summary = shown.summary {
                    Text(summary).font(.subheadline).foregroundStyle(.secondary)
                }
                if let cast = shown.cast, !cast.isEmpty {
                    Text(cast.joined(separator: ", ")).font(.caption).foregroundStyle(.secondary).lineLimit(2)
                }
                if let error { Text(error).font(.footnote).foregroundStyle(Brand.bad) }
                if let children = shown.children, !children.isEmpty {
                    VStack(alignment: .leading, spacing: 0) {
                        Text(shown.type == "show" ? "Seasons" : "Episodes").font(.title3.bold()).padding(.bottom, 4)
                        ForEach(children) { child in ItemRow(item: child).padding(.horizontal, -16) }
                    }
                }
                IndexFooter(item: shown) { Task { await load(fresh: true) } }
            }
            .padding(16)
        }
        .background(alignment: .top) {
            PlexArtwork(path: shown.art, aspect: 16.0 / 9.0, width: 800)
                .opacity(0.35)
                .mask(LinearGradient(colors: [.black, .clear], startPoint: .top, endPoint: .bottom))
                .ignoresSafeArea()
        }
        .background(Brand.ink.ignoresSafeArea())
        .navigationTitle(shown.title)
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            ToolbarItem(placement: .topBarTrailing) {
                Menu { ItemMenu(item: shown) } label: { Image(systemName: "ellipsis.circle") }
            }
        }
        // The copy first (instant), then a fresh one from the server.
        .task {
            await load(fresh: false)
            await load(fresh: true)
        }
        .refreshable {
            model.catalog(.refresh, shown)
            await load(fresh: true)
        }
    }

    private func load(fresh: Bool) async {
        do {
            full = try await model.couchd?.plexItem(item.key, fresh: fresh)
            error = nil
        } catch {
            if full == nil { self.error = error.localizedDescription }
        }
    }

    private var header: some View {
        HStack(alignment: .bottom, spacing: 14) {
            PlexArtwork(path: shown.type == "episode" ? shown.thumb : shown.poster,
                        aspect: shown.type == "episode" ? 16.0 / 9.0 : 2.0 / 3.0, width: 400)
                .frame(width: shown.type == "episode" ? 180 : 120)
            VStack(alignment: .leading, spacing: 4) {
                Text(shown.title).font(.title2.bold()).lineLimit(3)
                if !shown.subtitle.isEmpty { Text(shown.subtitle).foregroundStyle(.secondary) }
                Text(facts).font(.caption).foregroundStyle(.secondary)
                if shown.transcodes {
                    Label("HEVC: Plex converts it for the TV", systemImage: "arrow.triangle.2.circlepath")
                        .font(.caption).foregroundStyle(Brand.amber)
                }
            }
        }
    }

    @ViewBuilder private var actions: some View {
        if shown.isPlayable {
            VStack(spacing: 10) {
                Button { model.playOnTV(shown) } label: {
                    Label(resumeLabel(shown), systemImage: "play.fill").frame(maxWidth: .infinity, minHeight: 34)
                }
                .buttonStyle(.borderedProminent).tint(Brand.amber).foregroundStyle(Brand.onAmber)
                if shown.inProgress {
                    Button { model.playOnTV(shown, fromStart: true) } label: {
                        Label("Play from the start", systemImage: "backward.end.fill").frame(maxWidth: .infinity, minHeight: 30)
                    }
                    .buttonStyle(.bordered)
                }
            }
        } else {
            VStack(spacing: 10) {
                if let next = shown.upNext?.first {
                    // The obvious next step for a show: carry on watching.
                    Button { model.playOnTV(next) } label: {
                        VStack(spacing: 2) {
                            Label(next.inProgress ? "Resume \(episodeCode(next))" : "Play \(episodeCode(next))",
                                  systemImage: "play.fill").font(.headline)
                            Text(next.title).font(.caption).lineLimit(1)
                        }
                        .frame(maxWidth: .infinity, minHeight: 40)
                    }
                    .buttonStyle(.borderedProminent).tint(Brand.amber).foregroundStyle(Brand.onAmber)
                }
                Button { model.playOnTV(shown) } label: {
                    Label("Open on TV", systemImage: "tv").frame(maxWidth: .infinity, minHeight: 30)
                }
                .buttonStyle(.bordered)
            }
        }
    }

    private func episodeCode(_ item: PlexItem) -> String {
        String(format: "S%02dE%02d", item.season ?? 0, item.episode ?? 0)
    }

    private func resumeLabel(_ item: PlexItem) -> String {
        guard item.inProgress, let offset = item.offsetMs else { return "Play on TV" }
        return "Resume from \(Duration.milliseconds(offset).formatted(.time(pattern: offset >= 3_600_000 ? .hourMinuteSecond : .minuteSecond)))"
    }

    private var facts: String {
        var parts: [String] = []
        if let duration = shown.durationMs {
            parts.append(Duration.milliseconds(duration).formatted(.units(allowed: [.hours, .minutes], width: .narrow)))
        }
        if let rating = shown.contentRating { parts.append(rating) }
        if let resolution = shown.resolution { parts.append(resolution.lowercased() == "4k" ? "4K" : "\(resolution)p") }
        if let genres = shown.genres?.prefix(2), !genres.isEmpty { parts.append(genres.joined(separator: ", ")) }
        return parts.joined(separator: " · ")
    }
}

/// How current the TV box's copy of an entry is, and its artwork; quiet,
/// at the bottom of the detail screen.
private struct IndexFooter: View {
    @Environment(AppModel.self) private var model
    let item: PlexItem
    let reload: () -> Void

    var body: some View {
        if let index = item.index {
            VStack(alignment: .leading, spacing: 8) {
                Divider()
                HStack(spacing: 6) {
                    Image(systemName: index.stale ? "icloud.slash" : "checkmark.icloud")
                    Text(syncedText(index))
                }
                HStack(spacing: 6) {
                    Image(systemName: index.artCached ? "photo.badge.checkmark" : "photo")
                    Text(index.artCached ? "Artwork saved on the TV box" : "Artwork not saved yet")
                    Spacer()
                    if !index.artCached {
                        Button("Save") { model.catalog(.fetchArt, item) }.font(.caption.bold())
                    }
                }
            }
            .font(.caption)
            .foregroundStyle(.secondary)
        }
    }

    private func syncedText(_ index: PlexItem.IndexInfo) -> String {
        guard let synced = index.syncedAt else { return "Not in the index yet" }
        let when = Date(timeIntervalSince1970: synced).formatted(.relative(presentation: .named))
        return index.stale ? "Plex didn't answer · showing the copy from \(when)" : "Up to date with Plex · \(when)"
    }
}

/// One library as a poster grid, loaded a page at a time from the index.
struct SectionView: View {
    @Environment(AppModel.self) private var model
    let section: PlexSection
    @State private var items: [PlexItem] = []
    @State private var total = 0
    @AppStorage("sectionSort") private var sort: PlexSort = .added
    @State private var unwatched = false
    @State private var loading = false
    @State private var error: String?

    private let pageSize = 90

    var body: some View {
        ScrollView {
            if total > 0 {
                Text("\(total.formatted()) \(section.type == "show" ? "shows" : "movies")")
                    .font(.caption).foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, alignment: .leading).padding(.horizontal, 16)
            }
            LazyVGrid(columns: [GridItem(.adaptive(minimum: 104), spacing: 12)], spacing: 16) {
                ForEach(items) { item in
                    NavigationLink(value: item) { PosterCard(item: item) }
                        .buttonStyle(.plain)
                        .contextMenu { ItemMenu(item: item) } preview: { ItemPreview(item: item) }
                        .onAppear { if item.id == items.last?.id { Task { await loadMore() } } }
                }
            }
            .padding(16)
            if let error { Text(error).font(.footnote).foregroundStyle(Brand.bad).padding() }
            if loading { ProgressView().padding() }
        }
        .background(Brand.ink.ignoresSafeArea())
        .navigationTitle(section.title)
        .toolbar {
            Menu {
                Picker("Sort", selection: $sort) {
                    ForEach(PlexSort.allCases) { Text($0.label).tag($0) }
                }
                Toggle("Unwatched only", isOn: $unwatched)
            } label: {
                Image(systemName: unwatched ? "line.3.horizontal.decrease.circle.fill" : "line.3.horizontal.decrease.circle")
            }
        }
        .refreshable { await reload() }
        .task(id: "\(sort.rawValue)-\(unwatched)") { await reload() }
    }

    private func reload() async {
        items = []
        total = 0
        await loadMore()
    }

    private func loadMore() async {
        guard !loading, items.isEmpty || items.count < total, let couchd = model.couchd else { return }
        loading = true
        defer { loading = false }
        do {
            let page = try await couchd.plexSection(section.key, start: items.count, size: pageSize, sort: sort, unwatched: unwatched)
            total = page.total
            items += page.items.filter { new in !items.contains { $0.id == new.id } }
            error = nil
        } catch {
            self.error = error.localizedDescription
        }
    }
}
