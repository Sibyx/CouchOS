import Foundation
import Network

/// Kodi EventServer client (UDP 9777) for the mouse pointer.
///
/// UDP: no connection, no acknowledgement; each finger movement is one
/// 37-byte datagram. Kodi moves focus to the control under the cursor, so a
/// "click" is simply Input.Select on the JSON-RPC link.
final class EventServer {
    private enum PacketType: UInt16 {
        case helo = 0x01, bye = 0x02, mouse = 0x04, ping = 0x05
    }

    private var connection: NWConnection?
    private var sequence: UInt32 = 0
    private var keepAlive: Task<Void, Never>?
    private let uid = UInt32.random(in: 1...UInt32.max)

    func connect(host: String, port: UInt16 = 9777) {
        disconnect()
        guard let nwPort = NWEndpoint.Port(rawValue: port) else { return }
        let params = NWParameters.udp
        params.serviceClass = .interactiveVideo
        let conn = NWConnection(host: NWEndpoint.Host(host), port: nwPort, using: params)
        conn.start(queue: .main)
        connection = conn

        var helo = Data("CouchOS\0".utf8)
        helo.append(0)                          // icon type: none
        helo.append(contentsOf: [0, 0])         // port (unused)
        helo.append(contentsOf: [0, 0, 0, 0])   // reserved
        helo.append(contentsOf: [0, 0, 0, 0])   // reserved
        send(.helo, helo)

        // Kodi drops clients silent for 60 s.
        keepAlive = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(for: .seconds(20))
                self?.send(.ping, Data())
            }
        }
    }

    func disconnect() {
        keepAlive?.cancel()
        if connection != nil { send(.bye, Data()) }
        connection?.cancel()
        connection = nil
    }

    /// Absolute pointer position, 0...1 on each axis.
    func movePointer(x: Double, y: Double) {
        var payload = Data([0x01]) // MS_ABSOLUTE
        payload.appendBigEndian(UInt16((min(max(x, 0), 1) * 65_535).rounded()))
        payload.appendBigEndian(UInt16((min(max(y, 0), 1) * 65_535).rounded()))
        send(.mouse, payload)
    }

    private func send(_ type: PacketType, _ payload: Data) {
        guard let connection else { return }
        sequence &+= 1
        var packet = Data("XBMC".utf8)
        packet.append(contentsOf: [2, 0])          // protocol 2.0
        packet.appendBigEndian(type.rawValue)
        packet.appendBigEndian(sequence)
        packet.appendBigEndian(UInt32(1))          // single-packet message
        packet.appendBigEndian(UInt16(payload.count))
        packet.appendBigEndian(uid)
        packet.append(Data(count: 10))             // reserved
        packet.append(payload)
        connection.send(content: packet, completion: .idempotent)
    }
}

private extension Data {
    mutating func appendBigEndian<T: FixedWidthInteger>(_ value: T) {
        Swift.withUnsafeBytes(of: value.bigEndian) { append(contentsOf: $0) }
    }
}
