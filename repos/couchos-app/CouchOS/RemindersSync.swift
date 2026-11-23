import EventKit
import Foundation

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
final class RemindersSync {
    static let shared = RemindersSync()
    nonisolated static let source = "reminders"
    /// dashd keeps done items on the TV this long; older ones aren't sent.
    private static let doneWindow: TimeInterval = 12 * 3600
    /// dashd's MAX_ITEMS; it caps a sync anyway.
    private static let maxItems = 40

    private let store = EKEventStore()
    private var changed: (any NSObjectProtocol)?
    private var pending: Task<Void, Never>?

    private init() {}

    // MARK: settings

    /// The list shown on the TV (its calendarIdentifier); nil = off.
    var listID: String? {
        get { UserDefaults.standard.string(forKey: "remindersList") }
        set { UserDefaults.standard.set(newValue, forKey: "remindersList") }
    }

    var isOn: Bool { listID != nil && Self.hasAccess }

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

    var listTitle: String? {
        guard let listID, Self.hasAccess else { return nil }
        return store.calendar(withIdentifier: listID)?.title
    }

    /// Show `list` on the TV, or (nil) take the synced items down.
    func choose(_ list: String?) async throws -> Board? {
        listID = list
        guard list != nil else {
            return try await send([])
        }
        return try await sync()
    }

    // MARK: syncing

    /// Watch Reminders while the app runs (another device's change arrives
    /// through iCloud the same way).
    func observe() {
        guard changed == nil else { return }
        changed = NotificationCenter.default.addObserver(
            forName: .EKEventStoreChanged, object: store, queue: .main
        ) { _ in
            Task { @MainActor in RemindersSync.shared.syncSoon() }
        }
    }

    /// Several changes in a row become one sync.
    func syncSoon() {
        guard isOn else { return }
        pending?.cancel()
        pending = Task {
            try? await Task.sleep(for: .seconds(1))
            guard !Task.isCancelled else { return }
            _ = try? await sync()
        }
    }

    /// Sends the list to the TV. Answers the whole board.
    @discardableResult
    func sync() async throws -> Board? {
        guard let listID else { return nil }
        guard Self.hasAccess else { throw SyncError.noAccess }
        guard let list = store.calendar(withIdentifier: listID) else { throw SyncError.noList }
        let now = Date()
        let open = await fetch(store.predicateForIncompleteReminders(
            withDueDateStarting: nil, ending: nil, calendars: [list]))
        let done = await fetch(store.predicateForCompletedReminders(
            withCompletionDateStarting: now.addingTimeInterval(-Self.doneWindow), ending: now, calendars: [list]))
        let items = (open.sorted(by: Item.byDue) + done.sorted { ($0.doneAt ?? now) > ($1.doneAt ?? now) })
            .prefix(Self.maxItems)
        return try await send(Array(items))
    }

    /// Ticked in the app: written to Reminders, then synced.
    func setDone(_ externalID: String, _ done: Bool) async throws -> Board? {
        guard Self.hasAccess else { throw SyncError.noAccess }
        guard let reminder = store.calendarItem(withIdentifier: externalID) as? EKReminder else {
            throw SyncError.gone
        }
        reminder.isCompleted = done
        try store.save(reminder, commit: true)
        return try await sync()
    }

    private func send(_ items: [Item]) async throws -> Board {
        let host = UserDefaults.standard.string(forKey: "host") ?? ""
        guard let dashd = Dashd(host: host), !host.isEmpty else { throw SyncError.noBox }
        let payload: [[String: Any]] = items.map { item in
            var entry: [String: Any] = ["ext_id": item.id, "text": item.title, "done": item.done]
            if let at = item.doneAt { entry["done_at"] = Int(at.timeIntervalSince1970) }
            return entry
        }
        return try await dashd.board(["op": "sync", "source": Self.source, "items": payload])
    }

    // MARK: EventKit

    private struct Item: Sendable {
        let id: String
        let title: String
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

    private func fetch(_ predicate: NSPredicate) async -> [Item] {
        await withCheckedContinuation { continuation in
            store.fetchReminders(matching: predicate) { reminders in
                let items = (reminders ?? []).compactMap { reminder -> Item? in
                    let title = (reminder.title ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
                    guard !title.isEmpty else { return nil }
                    return Item(
                        id: reminder.calendarItemIdentifier,
                        title: title,
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

    enum SyncError: LocalizedError {
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
}
