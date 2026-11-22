import Foundation
import SwiftUI

/// The Plex catalog, browsed through couchd (`/api/plex/...`). couchd uses the
/// TV's own Plex sign-in (PM4K's), so the phone has no login and no token,
/// and sees the same user, Continue Watching and watched state as the TV.
/// Playing hands the item to Plex on the TV: the Pi streams it, not the phone.
struct PlexItem: Decodable, Identifiable, Hashable {
    let key: String
    let type: String
    let title: String
    var year: Int?
    var summary: String?
    var thumb: String?
    var poster: String?
    var art: String?
    var durationMs: Int?
    var offsetMs: Int?
    var watched: Bool?
    var show: String?
    var season: Int?
    var episode: Int?
    var leafCount: Int?
    var viewedLeafCount: Int?
    var childCount: Int?
    var contentRating: String?
    var rating: Double?
    var genres: [String]?
    var videoCodec: String?
    var resolution: String?
    var cast: [String]?
    var children: [PlexItem]?
    /// A show's or season's episode to play: the one in progress, else the
    /// first unwatched (couchd works it out from the index).
    var upNext: [PlexItem]?
    /// How current couchd's copy is (detail screens only).
    var index: IndexInfo?

    struct IndexInfo: Decodable, Hashable {
        var syncedAt: Double?
        var artCached: Bool
        var stale: Bool
    }

    var id: String { key }

    /// Movies and episodes play; shows and seasons have children to pick from.
    var isPlayable: Bool { type == "movie" || type == "episode" }

    var inProgress: Bool { (offsetMs ?? 0) > 0 }

    var progress: Double? {
        guard let offset = offsetMs, let duration = durationMs, duration > 0, offset > 0 else { return nil }
        return min(1, Double(offset) / Double(duration))
    }

    /// "Severance · S01E04", "2016", "19 episodes"
    var subtitle: String {
        switch type {
        case "episode":
            let code = String(format: "S%02dE%02d", season ?? 0, episode ?? 0)
            return [show, code].compactMap { $0 }.joined(separator: " · ")
        case "season", "show":
            guard let leafCount else { return year.map(String.init) ?? "" }
            let unwatched = unwatchedCount ?? 0
            let left = unwatched > 0 && unwatched < leafCount ? " · \(unwatched) unwatched" : ""
            return "\(leafCount) episodes\(left)"
        default:
            return year.map(String.init) ?? ""
        }
    }

    /// The Pi 3 can't decode HEVC: Plex converts it on the server.
    var transcodes: Bool { videoCodec?.lowercased() == "hevc" }

    var unwatchedCount: Int? {
        guard let leafCount, let viewedLeafCount else { return nil }
        return max(0, leafCount - viewedLeafCount)
    }
}

struct PlexHub: Decodable, Identifiable {
    let id: String
    let title: String
    let items: [PlexItem]
}

struct PlexSection: Decodable, Identifiable, Hashable {
    let key: String
    let title: String
    let type: String
    var count: Int?
    var id: String { key }
    var symbol: String { type == "show" ? "tv" : "film" }
}

struct PlexPage: Decodable {
    let total: Int
    let items: [PlexItem]
}

enum PlexSort: String, CaseIterable, Identifiable {
    case added, watched, title, year, rating
    var id: String { rawValue }
    var label: String {
        switch self {
        case .title: "Title"
        case .added: "Recently added"
        case .watched: "Recently watched"
        case .year: "Year"
        case .rating: "Rating"
        }
    }
}

/// Search results grouped by kind, best group first.
struct PlexSearch: Decodable {
    struct Group: Decodable, Identifiable {
        let type: String
        let title: String
        let items: [PlexItem]
        var id: String { type }
    }
    let groups: [Group]
    /// false: the index isn't built yet and these came from the server.
    let indexed: Bool
}

enum SearchScope: String, CaseIterable, Identifiable {
    case all, movie, show, episode
    var id: String { rawValue }
    var label: String {
        switch self {
        case .all: "All"
        case .movie: "Movies"
        case .show: "Shows"
        case .episode: "Episodes"
        }
    }
}

/// The index and artwork cache on the Pi (`GET /api/catalog`).
struct CatalogStatus: Decodable {
    struct Job: Decodable, Identifiable {
        let kind: String
        let label: String
        let done: Int
        let total: Int
        let lane: String
        var id: String { lane }
        var fraction: Double? { total > 0 ? Double(done) / Double(total) : nil }
    }
    struct Art: Decodable { let files: Int; let bytes: Int; let limitBytes: Int }
    struct Intervals: Decodable { let quick: Double; let full: Double }
    struct Section: Decodable, Identifiable {
        let key: String
        let title: String
        let type: String
        let enabled: Int
        let prefetchArt: Int
        var syncedAt: Double?
        var fullSyncedAt: Double?
        var status: String?
        var error: String?
        let count: Int
        /// Entries per kind: movie, show, season, episode.
        var counts: [String: Int]?
        var id: String { key }
        /// "451 movies", "3 shows · 30 episodes"
        var contents: String {
            let counts = counts ?? [:]
            if type == "show" {
                return "\((counts["show"] ?? 0).formatted()) shows · \((counts["episode"] ?? 0).formatted()) episodes"
            }
            return "\((counts["movie"] ?? count).formatted()) movies"
        }
        var isEnabled: Bool { enabled != 0 }
        var keepsArt: Bool { prefetchArt != 0 }
        var symbol: String { type == "show" ? "tv" : "film" }
    }
    let state: String
    let jobs: [Job]
    let queued: Int
    var error: String?
    var server: String?
    var syncedAt: Double?
    var fullSyncedAt: Double?
    let intervals: Intervals
    let counts: [String: Int]
    let dbBytes: Int
    let art: Art
    let sections: [Section]
}

private struct HubList: Decodable { let hubs: [PlexHub] }
private struct SectionList: Decodable { let sections: [PlexSection] }
private struct Queued: Decodable { let queued: Bool }

extension Couchd {
    func plexHubs() async throws -> [PlexHub] {
        try await get("plex/hubs", as: HubList.self).hubs
    }

    func plexSections() async throws -> [PlexSection] {
        try await get("plex/sections", as: SectionList.self).sections
    }

    func plexSection(_ key: String, start: Int, size: Int, sort: PlexSort, unwatched: Bool) async throws -> PlexPage {
        try await get("plex/sections/\(key)", query: [
            URLQueryItem(name: "start", value: String(start)),
            URLQueryItem(name: "size", value: String(size)),
            URLQueryItem(name: "sort", value: sort.rawValue),
            URLQueryItem(name: "unwatched", value: unwatched ? "1" : "0"),
        ])
    }

    func plexSearch(_ text: String, scope: SearchScope) async throws -> PlexSearch {
        try await get("plex/search", query: [URLQueryItem(name: "q", value: text), URLQueryItem(name: "type", value: scope.rawValue)])
    }

    /// fresh: couchd asks the server first (briefly), so what was just
    /// watched on the TV shows as watched; else its copy.
    func plexItem(_ key: String, fresh: Bool = false) async throws -> PlexItem {
        try await get("plex/items/\(key)", query: fresh ? [URLQueryItem(name: "fresh", value: "1")] : [])
    }

    // MARK: the index and artwork cache

    func catalogStatus() async throws -> CatalogStatus {
        try await get("catalog")
    }

    func catalogSync(section: String? = nil, full: Bool) async throws {
        var body: [String: Any] = ["full": full]
        if let section { body["section"] = section }
        let _: Queued = try await post("catalog/sync", body)
    }

    func catalogSection(_ key: String, enabled: Bool? = nil, keepArt: Bool? = nil) async throws {
        var body: [String: Any] = [:]
        if let enabled { body["enabled"] = enabled }
        if let keepArt { body["prefetch_art"] = keepArt }
        let _: OK = try await post("catalog/sections/\(key)", body)
    }

    /// Re-read an entry and everything under it from the server.
    func catalogRefresh(_ key: String) async throws {
        let _: Queued = try await post("catalog/items/\(key)/refresh")
    }

    /// Download ("fetch") or remove ("drop") the artwork of an entry and
    /// everything under it, or of a whole library.
    func catalogArt(item key: String? = nil, section: String? = nil, fetch: Bool) async throws {
        let scope = section != nil ? "sections/\(section!)" : "items/\(key ?? "")"
        let _: Queued = try await post("catalog/\(scope)/art", ["action": fetch ? "fetch" : "drop"])
    }

    func catalogClearArt() async throws {
        let _: Queued = try await post("catalog/art/clear")
    }

    func catalogReset() async throws {
        let _: OK = try await post("catalog/reset")
    }

    /// Server-resized artwork (couchd fetches it with the TV's token).
    func plexArt(_ path: String?, width: Int, height: Int) -> URL? {
        guard let path else { return nil }
        return base.appending(path: "plex/art").appending(queryItems: [
            URLQueryItem(name: "path", value: path),
            URLQueryItem(name: "w", value: String(width)),
            URLQueryItem(name: "h", value: String(height)),
        ])
    }
}

/// Poster or still from couchd, with a quiet placeholder while it loads.
struct PlexArtwork: View {
    @Environment(AppModel.self) private var model
    let path: String?
    var aspect: CGFloat = 2.0 / 3.0
    var width = 300

    var body: some View {
        let height = Int(CGFloat(width) / aspect)
        // The fixed-ratio box sets the size; the image fills it and is clipped.
        Brand.panel
            .aspectRatio(aspect, contentMode: .fit)
            .overlay {
                AsyncImage(url: model.couchd?.plexArt(path, width: width, height: height),
                           transaction: Transaction(animation: .easeOut(duration: 0.2))) { phase in
                    if let image = phase.image {
                        image.resizable().scaledToFill()
                    } else {
                        Image(systemName: "film").foregroundStyle(Brand.faint)
                    }
                }
            }
            .clipShape(RoundedRectangle(cornerRadius: Brand.Radius.small, style: .continuous))
    }
}
