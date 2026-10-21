import Foundation
import Network

/// Greenfield local relay: lets an iPhone on the same machine/LAN reach the
/// backend through this Mac hub. Raw TCP proxy via Network.framework.
///
/// - Forwards ONLY `/v1/device-gateway/*` and `/v1/voice/live` (WS upgrade
///   proxied bidirectionally as opaque bytes, so the handshake just works).
/// - Serves relay health locally: `GET /v1/relay/health` ->
///   `{ok,v,fp,backend_reachable}`.
/// - Strips hop-by-hop headers, never logs auth/ticket material.
/// - Disabled unless `EVIE_RELAY_ENABLED=1`. Binds 127.0.0.1 unless
///   `EVIE_RELAY_LAN_BIND=1`. Port from `EVIE_RELAY_PORT` (default 9411).
final class EvieRelayForwarder: Sendable {
    private let lock = NSLock()
    private var listener: NWListener?
    private var running = false
    private let backendHost: String
    private let backendPort: UInt16
    private let fingerprint: String

    init(backendBaseURL: URL, fingerprint: String = "evie-relay-1") {
        self.backendHost = backendBaseURL.host ?? "127.0.0.1"
        self.backendPort = UInt16(backendBaseURL.port ?? 8000)
        self.fingerprint = fingerprint
    }

    static var isEnabled: Bool {
        envFlag("EVIE_RELAY_ENABLED")
    }

    static var port: UInt16 {
        if let raw = ProcessInfo.processInfo.environment["EVIE_RELAY_PORT"],
           let p = UInt16(raw.trimmingCharacters(in: .whitespaces)), p > 0 { return p }
        return 9411
    }

    static var lanBind: Bool {
        envFlag("EVIE_RELAY_LAN_BIND")
    }

    func start() {
        lock.lock()
        guard !running else { lock.unlock(); return }
        running = true
        lock.unlock()
        guard Self.isEnabled else { return }
        let bindIP: NWEndpoint.Host = Self.lanBind ? "0.0.0.0" : "127.0.0.1"
        let params = NWParameters.tcp
        params.allowLocalEndpointReuse = true
        guard let listener = try? NWListener(using: params, on: NWEndpoint.Port(rawValue: Self.port)!) else { return }
        // NWListener binds all interfaces by default; restrict via advertised endpoint is not
        // supported directly, so LAN gating is enforced per-connection below.
        let queue = DispatchQueue(label: "evie.relay.listener")
        listener.newConnectionHandler = { [weak self] inbound in
            self?.handle(inbound: inbound, bindIP: bindIP)
        }
        listener.stateUpdateHandler = { [weak self] state in
            if case .failed = state { self?.lock.lock(); self?.running = false; self?.lock.unlock() }
        }
        listener.start(queue: queue)
        lock.lock()
        self.listener = listener
        lock.unlock()
    }

    func stop() {
        lock.lock()
        let l = listener
        listener = nil
        running = false
        lock.unlock()
        l?.cancel()
    }

    // MARK: - Per-connection

    private func handle(inbound: NWConnection, bindIP: NWEndpoint.Host) {
        // Enforce loopback-only unless LAN bind is on.
        if !Self.lanBind, let remote = inbound.endpoint as? NWEndpoint, case .hostPort(let host, _) = remote, host != .ipv4(.loopback), host != .name("localhost", nil) {
            inbound.cancel()
            return
        }
        let queue = DispatchQueue(label: "evie.relay.conn")
        inbound.stateUpdateHandler = { state in
            if case .ready = state { self.readRequest(on: inbound, queue: queue) }
            if case .failed = state { inbound.cancel() }
        }
        inbound.start(queue: queue)
    }

    private func readRequest(on inbound: NWConnection, queue: DispatchQueue) {
        inbound.receive(minimumIncompleteLength: 1, maximumLength: 65536) { [weak self] data, _, isComplete, error in
            guard let self, let data, !data.isEmpty, error == nil else { inbound.cancel(); return }
            var buffer = Data(data)
            // Accumulate until end of headers.
            func more() {
                inbound.receive(minimumIncompleteLength: 1, maximumLength: 65536) { chunk, _, done, err in
                    guard let chunk, !chunk.isEmpty, err == nil else { self.serveBadRequest(inbound); return }
                    buffer.append(chunk)
                    if self.headerEnd(in: buffer) != nil {
                        self.dispatchRequest(buffer, inbound: inbound, queue: queue)
                    } else if buffer.count > 256 * 1024 || done {
                        self.serveBadRequest(inbound)
                    } else { more() }
                }
            }
            if self.headerEnd(in: buffer) != nil {
                self.dispatchRequest(buffer, inbound: inbound, queue: queue)
            } else if isComplete {
                self.serveBadRequest(inbound)
            } else {
                more()
            }
        }
    }

    private func headerEnd(in data: Data) -> Int? {
        let sep: [UInt8] = [13, 10, 13, 10]
        guard data.count >= 4 else { return nil }
        for i in 0...(data.count - 4) {
            if data[i] == sep[0], data[i+1] == sep[1], data[i+2] == sep[2], data[i+3] == sep[3] { return i + 4 }
        }
        return nil
    }

    private func dispatchRequest(_ raw: Data, inbound: NWConnection, queue: DispatchQueue) {
        guard let headerEnd = headerEnd(in: raw),
              let headerText = String(data: raw.prefix(headerEnd), encoding: .utf8) ?? String(data: raw.prefix(headerEnd), encoding: .isoLatin1) else {
            serveBadRequest(inbound); return
        }
        let lines = headerText.components(separatedBy: "\r\n")
        guard let requestLine = lines.first else { serveBadRequest(inbound); return }
        let parts = requestLine.split(separator: " ")
        guard parts.count >= 2 else { serveBadRequest(inbound); return }
        let method = String(parts[0]).uppercased()
        var target = String(parts[1])
        if let q = target.firstIndex(of: "?") { target = String(target[..<q]) }

        if target == "/v1/relay/health" || target == "/v1/device-gateway/relay/health" {
            serveHealth(inbound); return
        }
        let allowed = target.hasPrefix("/v1/device-gateway/") || target == "/v1/voice/live" || target.hasPrefix("/v1/voice/live?")
        guard allowed else { serveNotFound(inbound); return }

        // Rebuild headers: strip hop-by-hop, rewrite Host to backend.
        var outLines: [String] = ["\(method) \(parts[1]) \(parts.count > 2 ? parts[2] : "HTTP/1.1")"]
        let hop: Set<String> = ["connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailer", "transfer-encoding", "upgrade", "proxy-connection"]
        // Upgrade/Connection must pass through for WS handshake.
        let wsPassthrough: Set<String> = ["upgrade", "connection"]
        for line in lines.dropFirst() {
            if line.isEmpty { continue }
            guard let colon = line.firstIndex(of: ":") else { continue }
            let name = line[..<colon].trimmingCharacters(in: .whitespaces).lowercased()
            if hop.contains(name), !wsPassthrough.contains(name) { continue }
            if name == "host" {
                outLines.append("Host: \(backendHost):\(backendPort)")
            } else {
                outLines.append(line)
            }
        }
        var out = (outLines.joined(separator: "\r\n") + "\r\n\r\n").data(using: .utf8)!
        out.append(raw.dropFirst(headerEnd))

        let backend = NWConnection(host: NWEndpoint.Host(backendHost), port: NWEndpoint.Port(rawValue: backendPort)!, using: .tcp)
        backend.stateUpdateHandler = { state in
            switch state {
            case .ready:
                backend.send(content: out, completion: .contentProcessed { _ in
                    self.pipe(inbound, backend, queue: queue)
                    self.pipe(backend, inbound, queue: queue)
                })
            case .failed, .cancelled:
                inbound.cancel()
            default: break
            }
        }
        backend.start(queue: queue)
    }

    private func pipe(_ from: NWConnection, _ to: NWConnection, queue: DispatchQueue) {
        from.receive(minimumIncompleteLength: 1, maximumLength: 65536) { data, _, isComplete, error in
            if let data, !data.isEmpty, error == nil {
                to.send(content: data, completion: .contentProcessed { _ in
                    if isComplete { to.send(content: nil, isComplete: true, completion: .idempotent) }
                    else { self.pipe(from, to, queue: queue) }
                })
            } else if isComplete || error != nil {
                to.send(content: nil, isComplete: true, completion: .idempotent)
            } else {
                self.pipe(from, to, queue: queue)
            }
        }
    }

    // MARK: - Local responses

    private func serveHealth(_ conn: NWConnection) {
        // Backend reachability: quick TCP probe with short timeout.
        let probe = NWConnection(host: NWEndpoint.Host(backendHost), port: NWEndpoint.Port(rawValue: backendPort)!, using: .tcp)
        let q = DispatchQueue(label: "evie.relay.health")
        var done = false
        func finish(_ reachable: Bool) {
            guard !done else { return }
            done = true
            probe.cancel()
            let body = "{\"ok\":true,\"v\":1,\"fp\":\"\(fingerprint)\",\"backend_reachable\":\(reachable ? "true" : "false")}"
            let resp = "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: \(body.utf8.count)\r\nConnection: close\r\n\r\n\(body)"
            conn.send(content: resp.data(using: .utf8), isComplete: true, completion: .idempotent)
        }
        probe.stateUpdateHandler = { state in
            switch state {
            case .ready: finish(true)
            case .failed, .cancelled: finish(false)
            default: break
            }
        }
        probe.start(queue: q)
        q.asyncAfter(deadline: .now() + 1.5) { finish(false) }
    }

    private func serveNotFound(_ conn: NWConnection) {
        let body = "{\"ok\":false,\"error\":\"not_found\"}"
        let resp = "HTTP/1.1 404 Not Found\r\nContent-Type: application/json\r\nContent-Length: \(body.utf8.count)\r\nConnection: close\r\n\r\n\(body)"
        conn.send(content: resp.data(using: .utf8), isComplete: true, completion: .idempotent)
    }

    private func serveBadRequest(_ conn: NWConnection) {
        let body = "{\"ok\":false,\"error\":\"bad_request\"}"
        let resp = "HTTP/1.1 400 Bad Request\r\nContent-Type: application/json\r\nContent-Length: \(body.utf8.count)\r\nConnection: close\r\n\r\n\(body)"
        conn.send(content: resp.data(using: .utf8), isComplete: true, completion: .idempotent)
    }

    private static func envFlag(_ key: String) -> Bool {
        let v = ProcessInfo.processInfo.environment[key]?.trimmingCharacters(in: .whitespaces).lowercased() ?? "0"
        return v == "1" || v == "true" || v == "yes" || v == "on"
    }
}
