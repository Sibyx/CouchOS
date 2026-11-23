import EventKit
import Foundation

/// Reminders, read with EventKit, for the two lists the TV shows: the to-do
/// list (`RemindersSync`) and the movies to watch (`MovieListSync`). One
/// store and one access prompt for both.
@MainActor
final class ReminderStore {
    static let shared = ReminderStore()

    let store = EKEventStore()
    private var changed: (any NSObjectProtocol)?

    private init() {}

    static var hasAccess: Bool { EKEventStore.authorizationStatus(for: .reminder) == .fullAccess }

    /// Asks once; afterwards only Settings can change the answer.
    func requestAccess() async -> Bool {
        if Self.hasAccess { return true }
        return (try? await store.requestFullAccessToReminders()) ?? false
    }

    struct List: Identifiable, Hashable {
        let id: String
        let title: String
    }

    func lists() -> [List] {
        guard Self.hasAccess else { return [] }
        return store.calendars(for: .reminder)
            .map { List(id: $0.calendarIdentifier, title: $0.title) }
            .sorted { $0.title.localizedStandardCompare($1.title) == .orderedAscending }
    }

    func calendar(_ id: String?) -> EKCalendar? {
        guard let id, Self.hasAccess else { return nil }
        return store.calendar(withIdentifier: id)
    }

    /// One reminder, copied out of EventKit (its objects aren't Sendable).
    struct Item: Sendable {
        let id: String
        let title: String
        let notes: String
        let url: String
        let done: Bool
        let doneAt: Date?
        let due: Date?
        let created: Date?

        /// Due ones first, soonest first; then oldest first, as added.
        static func byDue(_ a: Item, _ b: Item) -> Bool {
            switch (a.due, b.due) {
            case let (x?, y?): x < y
            case (.some, nil): true
            case (nil, .some): false
            case (nil, nil): (a.created ?? .distantPast) < (b.created ?? .distantPast)
            }
        }
    }

    /// Open reminders in `list`, and with `doneSince` those completed since then.
    func items(in list: EKCalendar, doneSince: Date? = nil) async -> (open: [Item], done: [Item]) {
        let open = await fetch(store.predicateForIncompleteReminders(withDueDateStarting: nil, ending: nil, calendars: [list]))
        guard let doneSince else { return (open, []) }
        let done = await fetch(store.predicateForCompletedReminders(
            withCompletionDateStarting: doneSince, ending: Date(), calendars: [list]))
        return (open, done)
    }

    private func fetch(_ predicate: NSPredicate) async -> [Item] {
        await withCheckedContinuation { continuation in
            store.fetchReminders(matching: predicate) { reminders in
                let items = (reminders ?? []).compactMap { reminder -> Item? in
                    let title = (reminder.title ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
                    let notes = reminder.notes ?? ""
                    let url = reminder.url?.absoluteString ?? ""
                    guard !title.isEmpty || !notes.isEmpty || !url.isEmpty else { return nil }
                    return Item(
                        id: reminder.calendarItemIdentifier,
                        title: title,
                        notes: notes,
                        url: url,
                        done: reminder.isCompleted,
                        doneAt: reminder.completionDate,
                        due: reminder.dueDateComponents.flatMap { Calendar.current.date(from: $0) },
                        created: reminder.creationDate
                    )
                }
                continuation.resume(returning: items)
            }
        }
    }

    /// Ticked in the app: written to Reminders.
    func setDone(_ id: String, _ done: Bool) throws {
        guard Self.hasAccess else { throw ReminderError.noAccess }
        guard let reminder = store.calendarItem(withIdentifier: id) as? EKReminder else { throw ReminderError.gone }
        reminder.isCompleted = done
        try store.save(reminder, commit: true)
    }

    /// Both lists follow Reminders while the app runs (another device's
    /// change arrives through iCloud the same way).
    func observe() {
        guard changed == nil else { return }
        changed = NotificationCenter.default.addObserver(
            forName: .EKEventStoreChanged, object: store, queue: .main
        ) { _ in
            Task { @MainActor in
                RemindersSync.shared.syncSoon()
                MovieListSync.shared.syncSoon()
            }
        }
    }

    /// The box the app last found (the intents run without the app's model).
    static func host() throws -> String {
        let host = UserDefaults.standard.string(forKey: "host") ?? ""
        guard !host.isEmpty else { throw ReminderError.noBox }
        return host
    }
}

enum ReminderError: LocalizedError {
    case noAccess, noList, noBox, gone

    var errorDescription: String? {
        switch self {
        case .noAccess: "CouchOS can't read Reminders. Allow it in Settings → Apps → CouchOS → Reminders."
        case .noList: "The Reminders list shown on the TV is gone. Choose another under Dashboard."
        case .noBox: "No TV box yet. Open CouchOS once at home so it can find it."
        case .gone: "That reminder no longer exists."
        }
    }
}

/// A Reminders list the TV mirrors, chosen in the app and kept by name in
/// UserDefaults. Several changes in a row become one sync.
@MainActor
class MirroredList {
    private let defaultsKey: String
    private var pending: Task<Void, Never>?

    init(defaultsKey: String) {
        self.defaultsKey = defaultsKey
    }

    /// The list (its calendarIdentifier); nil = off.
    var listID: String? {
        get { UserDefaults.standard.string(forKey: defaultsKey) }
        set { UserDefaults.standard.set(newValue, forKey: defaultsKey) }
    }

    var isOn: Bool { listID != nil && ReminderStore.hasAccess }

    var listTitle: String? { ReminderStore.shared.calendar(listID)?.title }

    func syncSoon() {
        guard isOn else { return }
        pending?.cancel()
        pending = Task {
            try? await Task.sleep(for: .seconds(1))
            guard !Task.isCancelled else { return }
            try? await syncNow()
        }
    }

    /// The chosen list, or an error saying why there is none.
    func list() throws -> EKCalendar {
        guard ReminderStore.hasAccess else { throw ReminderError.noAccess }
        guard let list = ReminderStore.shared.calendar(listID) else { throw ReminderError.noList }
        return list
    }

    /// Sends the list to the box (each kind its own way).
    func syncNow() async throws {}
}

/// One Reminders list, mirrored into the TV's TODO list (dashd's board
/// widget, op "sync", source "reminders"). Typed-in items stay as they are.
///
/// It syncs when the app comes to the front, on every Reminders change while
/// the app runs, after a tick in the app (written back to Reminders first),
/// and from the "Sync Reminders to TV" shortcut, which a Shortcuts
/// automation can run in the background ("When Reminders is closed").
/// Open reminders go first, then those done in the last 12 hours, which the
/// TV shows dimmed (dashd's keep_done_hours).
@MainActor
final class RemindersSync: MirroredList {
    static let shared = RemindersSync()
    nonisolated static let source = "reminders"
    /// dashd keeps done items on the TV this long; older ones aren't sent.
    private static let doneWindow: TimeInterval = 12 * 3600
    /// dashd's MAX_ITEMS; it caps a sync anyway.
    private static let maxItems = 40

    private init() { super.init(defaultsKey: "remindersList") }

    /// Show `list` on the TV, or (nil) take the synced items down.
    func choose(_ list: String?) async throws -> Board? {
        listID = list
        guard list != nil else { return try await send([]) }
        return try await sync()
    }

    override func syncNow() async throws { _ = try await sync() }

    /// Sends the list to the TV. Answers the whole board.
    @discardableResult
    func sync() async throws -> Board? {
        guard listID != nil else { return nil }
        let now = Date()
        let (open, done) = await ReminderStore.shared.items(in: try list(), doneSince: now.addingTimeInterval(-Self.doneWindow))
        let items = open.filter { !$0.title.isEmpty }.sorted(by: ReminderStore.Item.byDue)
            + done.filter { !$0.title.isEmpty }.sorted { ($0.doneAt ?? now) > ($1.doneAt ?? now) }
        return try await send(Array(items.prefix(Self.maxItems)))
    }

    /// Ticked in the app: written to Reminders, then synced.
    func setDone(_ externalID: String, _ done: Bool) async throws -> Board? {
        try ReminderStore.shared.setDone(externalID, done)
        return try await sync()
    }

    private func send(_ items: [ReminderStore.Item]) async throws -> Board {
        guard let dashd = Dashd(host: try ReminderStore.host()) else { throw ReminderError.noBox }
        let payload: [[String: Any]] = items.map { item in
            var entry: [String: Any] = ["ext_id": item.id, "text": item.title, "done": item.done]
            if let at = item.doneAt { entry["done_at"] = Int(at.timeIntervalSince1970) }
            return entry
        }
        return try await dashd.board(["op": "sync", "source": Self.source, "items": payload])
    }
}

/// The movies to watch: a shared Reminders list, one film per reminder
/// (title "The Matrix (1999)", a link in the notes: IMDb, TMDB or
/// Letterboxd). The box matches it to the Plex library and shows the films
/// it has on the TV's dashboard (couchd's POST /api/watchlist). Only open
/// reminders are sent: ticking one (watched) takes it off the TV.
@MainActor
final class MovieListSync: MirroredList {
    static let shared = MovieListSync()
    /// couchd's MAX_ENTRIES.
    private static let maxItems = 200

    private init() { super.init(defaultsKey: "moviesList") }

    /// Show `list` on the TV, or (nil) take the films down.
    func choose(_ list: String?) async throws -> Watchlist? {
        listID = list
        guard list != nil else { return try await send([]) }
        return try await sync()
    }

    override func syncNow() async throws { _ = try await sync() }

    /// Sends the open reminders to the box. Answers what it matched.
    @discardableResult
    func sync() async throws -> Watchlist? {
        guard listID != nil else { return nil }
        let (open, _) = await ReminderStore.shared.items(in: try list())
        return try await send(Array(open.sorted(by: ReminderStore.Item.byDue).prefix(Self.maxItems)))
    }

    /// What the box has now, without sending anything.
    func status() async throws -> Watchlist {
        guard let couchd = Couchd(host: try ReminderStore.host()) else { throw ReminderError.noBox }
        return try await couchd.get("watchlist")
    }

    /// Ticked in the app (watched): written to Reminders, then synced.
    func setDone(_ externalID: String) async throws -> Watchlist? {
        try ReminderStore.shared.setDone(externalID, true)
        return try await sync()
    }

    private func send(_ items: [ReminderStore.Item]) async throws -> Watchlist {
        guard let couchd = Couchd(host: try ReminderStore.host()) else { throw ReminderError.noBox }
        let payload: [[String: Any]] = items.map { ["id": $0.id, "title": $0.title, "notes": $0.notes, "url": $0.url] }
        return try await couchd.post("watchlist", ["items": payload])
    }
}

/// couchd's answer: each reminder, and whether the library has its film.
struct Watchlist: Decodable, Equatable {
    struct Entry: Decodable, Equatable, Identifiable {
        struct Match: Decodable, Equatable {
            let title: String?
            let year: Int?
        }

        let id: String
        let title: String
        var year: Int?
        let available: Bool
        var via: String?
        var matched: Match?
        var watched: Bool?
    }

    let items: [Entry]
    let shown: Int
}
