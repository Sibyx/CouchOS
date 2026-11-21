import Foundation

/// couchd JSON API (port 8765): screen mode, TV power, volume, Bluetooth.
/// Not latency-critical; one keep-alive URLSession.
struct Couchd {
    let base: URL

    init?(host: String, port: Int = 8765) {
        guard let url = URL(string: "http://\(host):\(port)/api/") else { return nil }
        base = url
    }

    private static let session: URLSession = {
        let config = URLSessionConfiguration.ephemeral
        config.timeoutIntervalForRequest = 5
        config.httpMaximumConnectionsPerHost = 2
        config.waitsForConnectivity = false
        return URLSession(configuration: config)
    }()

    private static let decoder: JSONDecoder = {
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        return decoder
    }()

    func get<T: Decodable>(_ path: String, as type: T.Type = T.self) async throws -> T {
        try await send(URLRequest(url: base.appending(path: path)))
    }

    func post<T: Decodable>(_ path: String, _ body: [String: Any] = [:], as type: T.Type = T.self) async throws -> T {
        var request = URLRequest(url: base.appending(path: path))
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONSerialization.data(withJSONObject: body)
        return try await send(request)
    }

    private func send<T: Decodable>(_ request: URLRequest) async throws -> T {
        let (data, response) = try await Self.session.data(for: request)
        if let http = response as? HTTPURLResponse, http.statusCode >= 400 {
            let message = (try? JSONDecoder().decode(APIError.self, from: data))?.error
            throw CouchdError.server(message ?? "HTTP \(http.statusCode)")
        }
        return try Self.decoder.decode(T.self, from: data)
    }
}

enum CouchdError: LocalizedError {
    case server(String)
    var errorDescription: String? {
        switch self { case .server(let message): message }
    }
}

struct APIError: Decodable { let error: String }
struct OK: Decodable {}

struct Status: Decodable {
    struct Kodi: Decodable { let running: Bool }
    struct AirPlay: Decodable { let running: Bool; let active: Bool }
    struct System: Decodable { let tempC: Double?; let memAvailableMb: Int; let throttled: String? }
    let kodi: Kodi
    let airplay: AirPlay
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
