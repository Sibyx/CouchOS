import Foundation
import Network

/// Persistent JSON-RPC link to Kodi's raw TCP server (port 9090).
///
/// One long-lived TCP connection with Nagle off: a key press is a single small
/// write, with no HTTP request, no new connection and no proxy hop. Kodi pushes
/// notifications (playback, keyboard requests) over the same socket.
final class KodiLink {
    enum State: Equatable {
        case idle, connecting, ready
        case failed(String)
    }

    var onState: ((State) -> Void)?
    var onNotification: ((String, [String: Any]) -> Void)?

    private(set) var state: State = .idle {
        didSet { if state != oldValue { onState?(state) } }
    }

    private var connection: NWConnection?
    private var host = ""
    private var port: UInt16 = 9090
    private var nextID = 1
    private var pending: [Int: (Any?) -> Void] = [:]
    private var buffer = Data()
    private var reconnect: Task<Void, Never>?

    func connect(host: String, port: UInt16 = 9090) {
        self.host = host
        self.port = port
        reconnect?.cancel()
        connection?.cancel()
        buffer.removeAll()
        failPending()

        let tcp = NWProtocolTCP.Options()
        tcp.noDelay = true
        tcp.enableKeepalive = true
        tcp.keepaliveIdle = 10
        tcp.connectionTimeout = 3
        let params = NWParameters(tls: nil, tcp: tcp)
        params.serviceClass = .responsiveData

        guard let nwPort = NWEndpoint.Port(rawValue: port) else { return }
        let conn = NWConnection(host: NWEndpoint.Host(host), port: nwPort, using: params)
        connection = conn
        state = .connecting
        conn.stateUpdateHandler = { [weak self] newState in
            MainActor.assumeIsolated { self?.handle(newState, on: conn) }
        }
        conn.start(queue: .main)
        receive(on: conn)
    }

    func disconnect() {
        reconnect?.cancel()
        connection?.cancel()
        connection = nil
        state = .idle
    }

    /// Fire a request. `reply` gets the JSON `result` (nil on error/disconnect).
    func call(_ method: String, _ params: [String: Any] = [:], reply: ((Any?) -> Void)? = nil) {
        guard state == .ready, let connection else {
            reply?(nil)
            return
        }
        let id = nextID
        nextID += 1
        if let reply { pending[id] = reply }
        let message: [String: Any] = ["jsonrpc": "2.0", "id": id, "method": method, "params": params]
        guard let data = try? JSONSerialization.data(withJSONObject: message) else { return }
        connection.send(content: data, completion: .contentProcessed { _ in })
    }

    // MARK: - connection

    private func handle(_ newState: NWConnection.State, on conn: NWConnection) {
        guard conn === connection else { return }
        switch newState {
        case .ready:
            state = .ready
        case .waiting(let error), .failed(let error):
            state = .failed(error.localizedDescription)
            scheduleReconnect()
        case .cancelled:
            failPending()
        default:
            break
        }
    }

    private func scheduleReconnect() {
        failPending()
        reconnect?.cancel()
        reconnect = Task { [weak self] in
            try? await Task.sleep(for: .seconds(1))
            guard !Task.isCancelled, let self else { return }
            self.connect(host: self.host, port: self.port)
        }
    }

    private func failPending() {
        let callbacks = pending.values
        pending.removeAll()
        callbacks.forEach { $0(nil) }
    }

    private func receive(on conn: NWConnection) {
        conn.receive(minimumIncompleteLength: 1, maximumLength: 65_536) { [weak self] data, _, isComplete, error in
            MainActor.assumeIsolated {
                guard let self, conn === self.connection else { return }
                if let data, !data.isEmpty {
                    self.buffer.append(data)
                    self.drainMessages()
                }
                if isComplete || error != nil {
                    self.state = .failed(error?.localizedDescription ?? "Kodi closed the connection")
                    self.scheduleReconnect()
                } else {
                    self.receive(on: conn)
                }
            }
        }
    }

    // MARK: - framing

    /// Kodi writes JSON objects back to back with no delimiter: split on the
    /// closing brace of each top-level object (ignoring braces inside strings).
    private func drainMessages() {
        var depth = 0
        var inString = false
        var escaped = false
        var start: Int?
        var consumed = 0
        let bytes = [UInt8](buffer)
        for (index, byte) in bytes.enumerated() {
            if inString {
                if escaped { escaped = false }
                else if byte == 0x5C { escaped = true }   // backslash
                else if byte == 0x22 { inString = false } // quote
                continue
            }
            switch byte {
            case 0x22:
                inString = true
            case 0x7B: // {
                if depth == 0 { start = index }
                depth += 1
            case 0x7D: // }
                depth -= 1
                if depth == 0, let begin = start {
                    dispatch(Data(bytes[begin...index]))
                    consumed = index + 1
                    start = nil
                }
            default:
                break
            }
        }
        if consumed > 0 { buffer.removeFirst(consumed) }
        if buffer.count > 4_000_000 { buffer.removeAll() } // garbage guard
    }

    private func dispatch(_ data: Data) {
        guard let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return }
        if let id = object["id"] as? Int {
            pending.removeValue(forKey: id)?(object["result"])
        } else if let method = object["method"] as? String {
            onNotification?(method, object["params"] as? [String: Any] ?? [:])
        }
    }
}
