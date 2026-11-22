import Foundation
import Network

/// Finds couchd over Bonjour (`_couchos._tcp`) and resolves it to an IPv4
/// address once, so later connections skip mDNS lookups entirely.
final class Discovery {
    var onFound: ((String) -> Void)?
    /// false: iOS refuses Local Network access (Settings → CouchOS), so
    /// nothing on the LAN can be found or reached. true once browsing works.
    var onAccess: ((Bool) -> Void)?

    /// The last box found, kept so clearing a typed-in host can go straight back to it.
    private(set) var found: String?

    private var browser: NWBrowser?
    private var probe: NWConnection?

    /// dns_sd's kDNSServiceErr_PolicyDenied: the user said no to Local Network.
    private static let policyDenied: Int32 = -65570

    func start() {
        guard browser == nil else { return }
        let browser = NWBrowser(for: .bonjour(type: "_couchos._tcp", domain: nil), using: .tcp)
        browser.stateUpdateHandler = { [weak self] state in
            MainActor.assumeIsolated {
                switch state {
                case .ready:
                    self?.onAccess?(true)
                case .waiting(let error), .failed(let error):
                    if case .dns(let code) = error, code == Self.policyDenied { self?.onAccess?(false) }
                default:
                    break
                }
            }
        }
        browser.browseResultsChangedHandler = { [weak self] results, _ in
            MainActor.assumeIsolated { self?.resolve(results) }
        }
        browser.start(queue: .main)
        self.browser = browser
    }

    func stop() {
        browser?.cancel()
        browser = nil
        probe?.cancel()
        probe = nil
    }

    private func resolve(_ results: Set<NWBrowser.Result>) {
        guard let result = results.first else { return }
        let params = NWParameters.tcp
        if let ip = params.defaultProtocolStack.internetProtocol as? NWProtocolIP.Options {
            ip.version = .v4
        }
        probe?.cancel()
        let conn = NWConnection(to: result.endpoint, using: params)
        conn.stateUpdateHandler = { [weak self] state in
            MainActor.assumeIsolated {
                guard let self, case .ready = state else { return }
                if case let .hostPort(host, _)? = conn.currentPath?.remoteEndpoint, let address = Self.address(host) {
                    self.found = address
                    self.onFound?(address)
                }
                conn.cancel()
            }
        }
        conn.start(queue: .main)
        probe = conn
    }

    private static func address(_ host: NWEndpoint.Host) -> String? {
        switch host {
        // Description carries the interface scope ("192.168.1.68%en0"); URLs can't.
        case .ipv4(let address): return "\(address)".split(separator: "%").first.map(String.init)
        case .name(let name, _): return name
        default: return nil
        }
    }
}
