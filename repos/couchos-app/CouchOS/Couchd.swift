import Foundation

/// couchd JSON API (port 8765): TV power, volume, Bluetooth, and the Plex
/// catalog (Plex.swift). Not latency-critical; one keep-alive URLSession.
struct Couchd {
    let base: URL

    init?(host: String, port: Int = 8765) {
        guard let url = URL(string: "http://\(host):\(port)/api/") else { return nil }
        base = url
    }

    private static let session: URLSession = {
        let config = URLSessionConfiguration.ephemeral
        // couchd answers browsing from its index on the Pi; only a first start
        // (nothing indexed yet) waits on the Plex server, which can be slow.
        config.timeoutIntervalForRequest = 15
        config.httpMaximumConnectionsPerHost = 4
        config.waitsForConnectivity = false
        return URLSession(configuration: config)
    }()

    private static let decoder: JSONDecoder = {
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        return decoder
    }()

    func get<T: Decodable>(_ path: String, query: [URLQueryItem] = [], as type: T.Type = T.self) async throws -> T {
        var url = base.appending(path: path)
        if !query.isEmpty { url.append(queryItems: query) }
        return try await send(URLRequest(url: url))
    }

    func post<T: Decodable>(_ path: String, _ body: [String: Any] = [:], as type: T.Type = T.self) async throws -> T {
        var request = URLRequest(url: base.appending(path: path))
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONSerialization.data(withJSONObject: body)
        return try await send(request)
    }

    /// Server-Sent Events from `/api/events`: one `Status` per `data:` line,
    /// sent at once, on every change, and as a heartbeat.
    func events() -> AsyncThrowingStream<Status, Error> {
        let url = base.appending(path: "events")
        return AsyncThrowingStream { continuation in
            let task = Task {
                do {
                    var request = URLRequest(url: url)
                    request.setValue("text/event-stream", forHTTPHeaderField: "Accept")
                    request.timeoutInterval = 40 // > heartbeat: a silent stream is a dead one
                    let (bytes, response) = try await Self.streamSession.bytes(for: request)
                    guard (response as? HTTPURLResponse)?.statusCode == 200 else {
                        throw CouchdError.server("events: HTTP \((response as? HTTPURLResponse)?.statusCode ?? 0)")
                    }
                    for try await line in bytes.lines where line.hasPrefix("data:") {
                        let json = line.dropFirst(5).trimmingCharacters(in: .whitespaces)
                        if let status = try? Self.decoder.decode(Status.self, from: Data(json.utf8)) {
                            continuation.yield(status)
                        }
                    }
                    continuation.finish()
                } catch {
                    continuation.finish(throwing: error)
                }
            }
            continuation.onTermination = { _ in task.cancel() }
        }
    }

    private static let streamSession: URLSession = {
        let config = URLSessionConfiguration.ephemeral
        config.timeoutIntervalForRequest = 40
        config.timeoutIntervalForResource = .infinity
        return URLSession(configuration: config)
    }()

    private func send<T: Decodable>(_ request: URLRequest) async throws -> T {
        let (data, response) = try await Self.session.data(for: request)
        if let http = response as? HTTPURLResponse, http.statusCode >= 400 {
            let message = (try? JSONDecoder().decode(APIError.self, from: data))?.error ?? "HTTP \(http.statusCode)"
            throw http.statusCode == 409 ? CouchdError.conflict(message) : CouchdError.server(message)
        }
        return try Self.decoder.decode(T.self, from: data)
    }
}

enum CouchdError: LocalizedError {
    case server(String)
    /// 409: the request clashes with what the TV is doing (AirPlay on screen).
    case conflict(String)
    var errorDescription: String? {
        switch self { case .server(let message), .conflict(let message): message }
    }
}

struct APIError: Decodable { let error: String }
struct OK: Decodable {}

struct Status: Decodable {
    struct Kodi: Decodable { let running: Bool }
    struct AirPlay: Decodable {
        let running: Bool
        let active: Bool
        /// screen (mirroring), video (from an app), audio
        var kind: String?
    }
    struct System: Decodable { let tempC: Double?; let memAvailableMb: Int; let throttled: String? }
    /// The Plex index on the Pi: idle, syncing (with what and how far), error.
    struct Catalog: Decodable, Equatable {
        let state: String
        var label: String?
        var done: Int?
        var total: Int?
        var fraction: Double? {
            guard let done, let total, total > 0 else { return nil }
            return Double(done) / Double(total)
        }
    }
    let kodi: Kodi
    let airplay: AirPlay
    var catalog: Catalog?
    let system: System
}

struct Audio: Decodable {
    let volume: Int?
    let muted: Bool
    let sink: String?
}

struct BluetoothState: Decodable {
    struct Device: Decodable, Identifiable {
        let mac: String
        let name: String
        let paired: Bool
        let connected: Bool
        let audio: Bool
        var id: String { mac }
    }
    let scanning: Bool
    let devices: [Device]
}
