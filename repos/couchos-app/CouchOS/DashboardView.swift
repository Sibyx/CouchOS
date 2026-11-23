import SwiftUI

/// The TV dashboard from the phone: the duck as the TV shows it, a message
/// for whoever is on the couch, and the shared TODO list. Changes show on
/// the TV at once (dashd notifies Kodi).
struct DashboardView: View {
    @Environment(AppModel.self) private var model
    @State private var board: Board?
    @State private var duck: DuckPreview?
    @State private var draft = ""
    @State private var newItem = ""
    @State private var busy = false
    @State private var remindersList: String?
    @State private var reminderLists: [RemindersSync.List] = []
    @FocusState private var addFocused: Bool

    var body: some View {
        Form {
            Section {
                if let duck, let url = model.dashd?.asset(duck.image) {
                    VStack(spacing: 10) {
                        AsyncImage(url: url) { image in
                            image.resizable().scaledToFit()
                        } placeholder: {
                            RoundedRectangle(cornerRadius: Brand.Radius.large).fill(Brand.raised)
                        }
                        .frame(maxWidth: 260, maxHeight: 260)
                        .accessibilityLabel("The duck, dressed for today's weather")
                        Text(duck.tip).font(.headline).multilineTextAlignment(.center)
                    }
                    .frame(maxWidth: .infinity)
                    .listRowBackground(Color.clear)
                }
                Button { model.showDashboard() } label: { Label("Show on TV now", systemImage: "tv") }
                    .disabled(!model.hasHost)
            }

            Section {
                TextField("A message for the TV", text: $draft, axis: .vertical)
                    .lineLimit(2...6)
                    .onChange(of: draft) { _, text in
                        if text.count > Board.messageLimit { draft = String(text.prefix(Board.messageLimit)) }
                    }
                HStack {
                    Button("Send to TV") { sendMessage(draft) }
                        .disabled(draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || busy)
                    Spacer()
                    if board?.message != nil {
                        Button("Take down", role: .destructive) { sendMessage("") }
                    }
                }
                .buttonStyle(.borderless)
            } header: {
                Text("Message")
            } footer: {
                if let message = board?.message {
                    Text("On the TV's dashboard now: “\(message.text)”")
                } else {
                    Text("\(draft.count)/\(Board.messageLimit). Shown large on the TV's dashboard until you take it down. Emoji show in the app only; the TV's font has none.")
                }
            }

            Section {
                ForEach(board?.items ?? []) { item in
                    Button { toggle(item) } label: {
                        HStack(spacing: 12) {
                            Image(systemName: item.done ? "checkmark.circle.fill" : "circle")
                                .foregroundStyle(item.done ? Brand.ok : Brand.muted)
                                .font(.title3)
                            Text(item.text)
                                .strikethrough(item.done)
                                .foregroundStyle(item.done ? Brand.muted : Brand.text)
                            if item.fromReminders {
                                Spacer()
                                Image(systemName: "checklist")
                                    .foregroundStyle(Brand.muted)
                                    .accessibilityLabel("From Reminders")
                            }
                        }
                    }
                    .swipeActions {
                        // A synced item would come back with the next sync:
                        // it is removed in Reminders, not here.
                        if !item.fromReminders {
                            Button("Delete", role: .destructive) { run(["op": "remove", "id": item.id]) }
                        }
                    }
                    .accessibilityValue(item.done ? "Done" : "To do")
                }
                HStack {
                    TextField("Add an item", text: $newItem)
                        .focused($addFocused)
                        .submitLabel(.done)
                        .onSubmit(add)
                    Button(action: add) { Image(systemName: "plus.circle.fill").font(.title3) }
                        .buttonStyle(.borderless)
                        .disabled(newItem.trimmingCharacters(in: .whitespaces).isEmpty)
                        .accessibilityLabel("Add")
                }
                if board?.items.contains(where: \.done) == true {
                    Button("Clear done items") { run(["op": "clear_done"]) }
                }
            } header: {
                Text("To do")
            } footer: {
                Text(remindersList == nil
                     ? "Done items stay on the TV, dimmed, for a while. Swipe to delete."
                     : "Done items stay on the TV, dimmed, for a while. Swipe to delete; items from Reminders are deleted in Reminders.")
            }

            Section {
                if remindersList == nil {
                    Button { Task { await startReminders() } } label: {
                        Label("Show a Reminders list on the TV", systemImage: "checklist")
                    }
                    .disabled(!model.hasHost || busy)
                    // Synced items left behind (Reminders access was turned
                    // off): taking them down needs no access.
                    if board?.items.contains(where: \.fromReminders) == true {
                        Button("Remove Reminders items from the TV", role: .destructive) { choose(nil) }
                            .disabled(busy)
                    }
                } else {
                    Picker("List", selection: Binding(
                        get: { remindersList ?? "" },
                        set: { choose($0) }
                    )) {
                        ForEach(reminderLists) { Text($0.title).tag($0.id) }
                    }
                    Button("Sync now") { choose(remindersList) }
                        .disabled(busy)
                    Button("Stop showing Reminders", role: .destructive) { choose(nil) }
                        .disabled(busy)
                }
            } header: {
                Text("Reminders")
            } footer: {
                Text(remindersList == nil
                     ? "Its open reminders join the TV's to-do list, and stay in step whenever CouchOS is open."
                     : "Syncs whenever CouchOS is open. To keep the TV current otherwise, add a Shortcuts automation: “When Reminders is closed” → “Sync Reminders to TV”.")
            }
        }
        .navigationTitle("Dashboard")
        .navigationBarTitleDisplayMode(.inline)
        .task { await load() }
        .refreshable { await load() }
    }

    private func load() async {
        let reminders = RemindersSync.shared
        remindersList = reminders.isOn ? reminders.listID : nil
        reminderLists = reminders.lists()
        guard let dashd = model.dashd else { return }
        do {
            // Synced first, so the list opens already in step with Reminders.
            if reminders.isOn, let synced = try? await reminders.sync() {
                board = synced
            } else {
                board = try await dashd.board()
            }
            duck = try? await dashd.duck()
        } catch {
            model.message = "The dashboard isn't reachable: \(error.localizedDescription)"
        }
    }

    private func toggle(_ item: Board.Item) {
        guard item.fromReminders, let extId = item.extId else {
            run(["op": "done", "id": item.id, "done": !item.done])
            return
        }
        reminders { try await $0.setDone(extId, !item.done) }
    }

    private func startReminders() async {
        guard await RemindersSync.shared.requestAccess() else {
            model.message = RemindersSync.SyncError.noAccess.localizedDescription
            return
        }
        reminderLists = RemindersSync.shared.lists()
        guard let first = reminderLists.first else {
            model.message = "There are no lists in Reminders yet."
            return
        }
        choose(first.id)
    }

    private func choose(_ list: String?) {
        remindersList = list
        reminders { try await $0.choose(list) }
        if list != nil { RemindersSync.shared.observe() }
    }

    private func reminders(_ work: @escaping @MainActor (RemindersSync) async throws -> Board?) {
        busy = true
        Task {
            defer { busy = false }
            do {
                if let updated = try await work(RemindersSync.shared) { board = updated }
            } catch {
                model.message = error.localizedDescription
            }
        }
    }

    private func add() {
        let text = newItem.trimmingCharacters(in: .whitespaces)
        guard !text.isEmpty else { return }
        newItem = ""
        run(["op": "add", "text": String(text.prefix(Board.itemLimit))])
        addFocused = true
    }

    private func sendMessage(_ text: String) {
        run(["op": "message", "text": text]) { draft = "" }
    }

    private func run(_ op: [String: Any], then done: @escaping () -> Void = {}) {
        guard let dashd = model.dashd else { return }
        busy = true
        Task {
            defer { busy = false }
            do {
                board = try await dashd.board(op)
                done()
            } catch {
                model.message = error.localizedDescription
            }
        }
    }
}
