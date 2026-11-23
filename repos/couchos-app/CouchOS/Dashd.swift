import Foundation

/// dashd JSON API (port 8766): the TV dashboard's widgets. The app writes
/// the message and the TODO list (widget "board") and previews the duck
/// (widget "weather"). See dashd/README.md.
struct Dashd {
    let base: URL

    init?(host: String, port: Int = 8766) {
        guard let url = URL(string: "http://\(host):\(port)/v1/") else { return nil }
        base = url
    }

    private static let session: URLSession = {
        let config = URLSessionConfiguration.ephemeral
        config.timeoutIntervalForRequest = 8
        config.waitsForConnectivity = false
        return URLSession(configuration: config)
    }()

    private static let decoder: JSONDecoder = {
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        return decoder
    }()

    func asset(_ path: String) -> URL? {
        URL(string: path, relativeTo: base)?.absoluteURL
    }

    func board(id: String = "board") async throws -> Board {
        try await send(URLRequest(url: base.appending(path: "widgets/\(id)")), as: Envelope<Board>.self).data
    }

    /// One board operation: {"op": "add", "text": ...} and so on. Answers the whole board.
    func board(_ op: [String: Any], id: String = "board") async throws -> Board {
        var request = URLRequest(url: base.appending(path: "widgets/\(id)"))
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONSerialization.data(withJSONObject: op)
        return try await send(request, as: Envelope<Board>.self).data
    }

    /// Input for any widget, e.g. {"op": "set", "mode": "night"} for "mode";
    /// the answer is not needed.
    func post(widget id: String, _ body: [String: Any]) async throws {
        var request = URLRequest(url: base.appending(path: "widgets/\(id)"))
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONSerialization.data(withJSONObject: body)
        _ = try await send(request, as: Ignored.self)
    }

    private struct Ignored: Decodable {}

    func duck(id: String = "weather") async throws -> DuckPreview? {
        try await send(URLRequest(url: base.appending(path: "widgets/\(id)")), as: Envelope<WeatherData>.self).data.duck
    }

    private func send<T: Decodable>(_ request: URLRequest, as type: T.Type) async throws -> T {
        let (data, response) = try await Self.session.data(for: request)
        if let http = response as? HTTPURLResponse, http.statusCode >= 400 {
            let message = (try? JSONDecoder().decode(APIError.self, from: data))?.error ?? "HTTP \(http.statusCode)"
            throw CouchdError.server(message)
        }
        return try Self.decoder.decode(T.self, from: data)
    }
}

private struct Envelope<T: Decodable>: Decodable {
    let data: T
}

struct Board: Decodable, Equatable {
    struct Message: Decodable, Equatable {
        let text: String
        var from: String?
        let at: Int
    }

    struct Item: Decodable, Equatable, Identifiable {
        let id: Int
        let text: String
        let done: Bool
        /// "reminders" for an item synced from Reminders; nil = typed in.
        var source: String?
        /// Its reminder's calendarItemIdentifier.
        var extId: String?

        var fromReminders: Bool { source == RemindersSync.source && extId != nil }
    }

    var message: Message?
    let items: [Item]

    /// Limits dashd enforces.
    static let messageLimit = 280
    static let itemLimit = 80
}

private struct WeatherData: Decodable {
    var duck: DuckPreview?
}

struct DuckPreview: Decodable, Equatable {
    /// /v1/assets/duck-<hash>.png, the same picture as on the TV.
    let image: String
    let tip: String
}
