import AppIntents
import Foundation

// Siri and the Shortcuts app: each action runs in the background, without
// opening the app, against the box the app last found (the "host" it keeps).
// No home hub is needed: Shortcuts' personal automations (time of day, an
// alarm stopped, joining the home Wi-Fi, Reminders closed) can run them too.
// They work at home only, like the app: the box is on the local network.

private func box() throws -> (couchd: Couchd, dashd: Dashd) {
    let host = UserDefaults.standard.string(forKey: "host") ?? ""
    guard !host.isEmpty, let couchd = Couchd(host: host), let dashd = Dashd(host: host) else {
        throw IntentError("No TV box yet. Open CouchOS once at home so it can find it.")
    }
    return (couchd, dashd)
}

struct IntentError: Error, CustomLocalizedStringResourceConvertible {
    let text: String
    init(_ text: String) { self.text = text }
    var localizedStringResource: LocalizedStringResource { "\(text)" }
}

struct ShowDashboardIntent: AppIntent {
    static var title: LocalizedStringResource { "Show Dashboard on TV" }
    static var description: IntentDescription {
        "Turns the TV on and shows the CouchOS dashboard: clock, weather, message and to-do list."
    }

    func perform() async throws -> some IntentResult {
        do {
            let _: OK = try await box().couchd.post("dashboard", ["show": true])
        } catch CouchdError.conflict {
            throw IntentError("A video is playing on the TV. Stop it first.")
        }
        return .result()
    }
}

enum TVPower: String, AppEnum {
    case on, standby

    static var typeDisplayRepresentation: TypeDisplayRepresentation { "TV Power" }
    static var caseDisplayRepresentations: [TVPower: DisplayRepresentation] {
        [.on: "On", .standby: "Standby"]
    }
}

struct TVPowerIntent: AppIntent {
    static var title: LocalizedStringResource { "Turn TV On or Off" }
    static var description: IntentDescription { "Turns the TV on, or puts it to standby, over HDMI-CEC." }

    @Parameter(title: "Power", default: .on)
    var power: TVPower

    static var parameterSummary: some ParameterSummary { Summary("Turn the TV \(\.$power)") }

    func perform() async throws -> some IntentResult {
        let _: OK = try await box().couchd.post("tv", ["power": power.rawValue])
        return .result()
    }
}

struct DashboardModeIntent: AppIntent {
    static var title: LocalizedStringResource { "Set Dashboard Mode" }
    static var description: IntentDescription {
        "Sets the dashboard's time of day (day, evening, night, or a mode of your own) until the schedule next changes. “auto” goes back to the schedule."
    }

    @Parameter(title: "Mode", default: "night")
    var mode: String

    static var parameterSummary: some ParameterSummary { Summary("Set the dashboard to \(\.$mode)") }

    func perform() async throws -> some IntentResult {
        let name = mode.trimmingCharacters(in: .whitespaces).lowercased()
        let op: [String: Any] = name == "auto" ? ["op": "auto"] : ["op": "set", "mode": name]
        try await box().dashd.post(widget: "mode", op)
        return .result()
    }
}

struct SyncRemindersIntent: AppIntent {
    static var title: LocalizedStringResource { "Sync Reminders to TV" }
    static var description: IntentDescription {
        "Sends the Reminders list chosen in CouchOS (Dashboard → Reminders) to the TV's to-do list."
    }

    @MainActor
    func perform() async throws -> some IntentResult {
        let reminders = RemindersSync.shared
        guard reminders.listID != nil else {
            throw IntentError("No Reminders list is shown on the TV. Choose one in CouchOS under Dashboard → Reminders.")
        }
        try await reminders.sync()
        return .result()
    }
}

struct CouchOSShortcuts: AppShortcutsProvider {
    static var appShortcuts: [AppShortcut] {
        AppShortcut(
            intent: ShowDashboardIntent(),
            phrases: ["Show the dashboard in \(.applicationName)", "\(.applicationName) dashboard"],
            shortTitle: "Show Dashboard",
            systemImageName: "rectangle.3.group"
        )
        AppShortcut(
            intent: TVPowerIntent(),
            phrases: ["Turn the TV on with \(.applicationName)", "Switch the TV with \(.applicationName)"],
            shortTitle: "TV Power",
            systemImageName: "tv"
        )
        AppShortcut(
            intent: DashboardModeIntent(),
            phrases: ["Good night in \(.applicationName)", "Set the \(.applicationName) dashboard mode"],
            shortTitle: "Dashboard Mode",
            systemImageName: "moon"
        )
        AppShortcut(
            intent: SyncRemindersIntent(),
            phrases: ["Sync reminders to \(.applicationName)", "Send my reminders to \(.applicationName)"],
            shortTitle: "Sync Reminders",
            systemImageName: "checklist"
        )
    }
}
