import Foundation
import NIOCore
import NIOPosix
import NIOSSH
import NIOSSL
import NIOTLS
import CommsCore

public final class CommsClient {
    private static let group = MultiThreadedEventLoopGroup(numberOfThreads: 1)
    private let loop: EventLoop
    // Recursive to permit a callback to stop its own client. A returning stop guarantees
    // that no callback from this client can subsequently begin.
    private let lock = NSRecursiveLock()
    private var route: RouteEndpoints?
    private let credentials: CredentialVault
    private var epoch: UInt64 = 0
    private var active = false
    private var roots: [Channel] = []
    private let session: String
    private let options: TransportOptions
    private let onStatus: (String) -> Void
    private let onResult: (GestureResult) -> Void
    // The following state belongs exclusively to loop.
    private var attempt: ConnectionAttempt?
    private var retry: Scheduled<Void>?
    private var retryDelay: TimeAmount

    public convenience init(route: SSHRoute, session: String, onStatus: @escaping (String) -> Void, onResult: @escaping (GestureResult) -> Void) {
        self.init(route: route, session: session, options: TransportOptions(), onStatus: onStatus, onResult: onResult)
    }
    init(route: SSHRoute, session: String, options: TransportOptions, onStatus: @escaping (String) -> Void, onResult: @escaping (GestureResult) -> Void) {
        self.route = RouteEndpoints(route); self.session = session; self.options = options
        credentials = CredentialVault(board: route.boardPassword, jump: route.jump?.password)
        self.onStatus = onStatus; self.onResult = onResult
        loop = Self.group.next(); retryDelay = options.retryMinimum
    }

    /// Idempotent. A stopped client discards credentials and must be replaced to reconnect.
    public func start() {
        lock.lock()
        guard !active, route != nil else { lock.unlock(); return }
        active = true; epoch &+= 1; let token = epoch
        lock.unlock()
        loop.execute { [weak self] in self?.connect(epoch: token) }
    }

    public func stop() {
        lock.lock()
        active = false; epoch &+= 1; route = nil; credentials.clear()
        let owned = roots; roots.removeAll()
        lock.unlock()
        owned.forEach { $0.close(promise: nil) }
        loop.execute { [weak self] in
            self?.retry?.cancel(); self?.retry = nil
            self?.attempt?.cancel(); self?.attempt = nil
        }
    }

    deinit { credentials.clear(); roots.forEach { $0.close(promise: nil) }; retry?.cancel(); attempt?.cancel() }

    private func current(_ token: UInt64) -> Bool {
        lock.lock(); defer { lock.unlock() }; return active && epoch == token
    }
    private func emitStatus(_ status: String, epoch token: UInt64) {
        lock.lock(); defer { lock.unlock() }
        if active && epoch == token { onStatus(status) }
    }
    private func emitResult(_ result: GestureResult, epoch token: UInt64) {
        lock.lock(); defer { lock.unlock() }
        if active && epoch == token { onResult(result) }
    }

    private func connect(epoch token: UInt64) {
        lock.lock()
        guard active && epoch == token, let route else { lock.unlock(); return }
        lock.unlock()
        let next = ConnectionAttempt()
        attempt = next
        emitStatus("Connecting", epoch: token)
        guard current(token) else { next.cancel(); return }
        do {
            _ = try CommsProtocol.subscribe(session: session)
            guard !route.boardHost.isEmpty, !route.boardUser.isEmpty, credentials.password(jumpHop: false)?.isEmpty == false,
                  route.caPEM.utf8.count <= 65_536, !route.caPEM.contains("PRIVATE KEY") else { throw TransportFailure.invalidConfiguration }
            let roots = try NIOSSLCertificate.fromPEMBytes(Array(route.caPEM.utf8))
            guard !roots.isEmpty else { throw TransportFailure.invalidConfiguration }
            var configuration = TLSConfiguration.makeClientConfiguration()
            configuration.trustRoots = .certificates(roots)
            configuration.additionalTrustRoots = []
            configuration.certificateVerification = .fullVerification
            configuration.minimumTLSVersion = .tlsv12
            let tls = try NIOSSLContext(configuration: configuration)
            let boardPin = try PinnedHostKey(route.boardHostKey)
            let jumpPin = try route.jump.map { try PinnedHostKey($0.hostKey) }
            next.deadline = loop.scheduleTask(in: options.connectTimeout) { [weak self, weak next] in
                guard let next else { return }; self?.failed(next, epoch: token, error: TransportFailure.deadline)
            }
            let bootstrap = ClientBootstrap(group: loop)
                .connectTimeout(options.connectTimeout)
                .resolver(options.resolver)
                .channelInitializer { [weak self] channel in
                    guard let self else { return channel.eventLoop.makeFailedFuture(TransportFailure.disconnected) }
                    self.lock.lock()
                    guard self.active && self.epoch == token && next.active else {
                        self.lock.unlock(); channel.close(promise: nil)
                        return channel.eventLoop.makeFailedFuture(TransportFailure.disconnected)
                    }
                    self.roots.append(channel); next.register(channel)
                    self.lock.unlock()
                    return channel.eventLoop.makeCompletedFuture {
                        let hop = route.jump
                        let ssh = self.ssh(channel: channel, username: hop?.user ?? route.boardUser, jumpHop: hop != nil, pin: jumpPin ?? boardPin)
                        try channel.pipeline.syncOperations.addHandler(ssh)
                    }
                }
            bootstrap.connect(host: route.jump?.host ?? route.boardHost, port: route.jump == nil ? options.boardPort : options.jumpPort)
                .flatMap { [weak self] channel -> EventLoopFuture<Channel> in
                    guard let self, self.current(token), next.active else { channel.close(promise: nil); return channel.eventLoop.makeFailedFuture(TransportFailure.disconnected) }
                    // Happy Eyeballs may create and discard multiple candidates. Only its
                    // selected connection may fail the logical SSH attempt.
                    do {
                        try channel.pipeline.syncOperations.addHandler(ConnectionEnd { [weak self, weak next] error in
                            guard let next else { return }; self?.failed(next, epoch: token, error: error)
                        })
                    } catch { return channel.eventLoop.makeFailedFuture(error) }
                    if route.jump == nil { return channel.eventLoop.makeSucceededFuture(channel) }
                    return self.direct(parent: channel, host: route.boardHost, port: self.options.boardPort) { child in
                        child.eventLoop.makeCompletedFuture {
                            try child.pipeline.syncOperations.addHandler(SSHByteStream())
                            try child.pipeline.syncOperations.addHandler(self.ssh(channel: child, username: route.boardUser, jumpHop: false, pin: boardPin))
                            try child.pipeline.syncOperations.addHandler(ConnectionEnd { [weak self, weak next] error in
                                guard let next else { return }; self?.failed(next, epoch: token, error: error)
                            })
                        }
                    }
                }.flatMap { [weak self] board -> EventLoopFuture<Channel> in
                    guard let self, self.current(token), next.active else { return board.eventLoop.makeFailedFuture(TransportFailure.disconnected) }
                    return self.direct(parent: board, host: "127.0.0.1", port: self.options.servicePort) { child in
                        child.eventLoop.makeCompletedFuture {
                            try child.pipeline.syncOperations.addHandler(SSHByteStream())
                            try child.pipeline.syncOperations.addHandler(try NIOSSLClientHandler(context: tls, serverHostname: "ultra96.week7.internal"))
                            try child.pipeline.syncOperations.addHandler(Subscriber(session: self.session, options: self.options, onSubscribed: { [weak self, weak next] in
                                guard let self, let next, next.active, self.current(token) else { return }
                                next.deadline?.cancel(); next.deadline = nil
                                self.emitStatus("Subscribed", epoch: token)
                            }, onResult: { [weak self, weak next] result in
                                guard let self, let next, next.active, self.current(token) else { return }
                                self.retryDelay = self.options.retryMinimum
                                self.emitResult(result, epoch: token)
                            }, onFailure: { [weak self, weak next] error in
                                guard let next else { return }; self?.failed(next, epoch: token, error: error)
                            }))
                        }
                    }
                }.whenFailure { [weak self, weak next] error in
                    guard let next else { return }; self?.failed(next, epoch: token, error: error)
                }
        } catch { failed(next, epoch: token, error: error) }
    }

    private func ssh(channel: Channel, username: String, jumpHop: Bool, pin: PinnedHostKey) -> NIOSSHHandler {
        NIOSSHHandler(role: .client(.init(userAuthDelegate: PasswordAuthentication(username: username, credentials: credentials, jumpHop: jumpHop), serverAuthDelegate: pin)), allocator: channel.allocator, inboundChildChannelInitializer: { channel, _ in
            channel.eventLoop.makeFailedFuture(TransportFailure.invalidData)
        })
    }

    private func direct(parent: Channel, host: String, port: Int, initialize: @escaping (Channel) -> EventLoopFuture<Void>) -> EventLoopFuture<Channel> {
        do {
            let ssh = try parent.pipeline.syncOperations.handler(type: NIOSSHHandler.self)
            let promise = parent.eventLoop.makePromise(of: Channel.self)
            let origin = try SocketAddress(ipAddress: "127.0.0.1", port: 0)
            ssh.createChannel(promise, channelType: .directTCPIP(.init(targetHost: host, targetPort: port, originatorAddress: origin))) { child, type in
                guard case .directTCPIP = type else { return child.eventLoop.makeFailedFuture(TransportFailure.invalidData) }
                return initialize(child)
            }
            return promise.futureResult
        } catch { return parent.eventLoop.makeFailedFuture(error) }
    }

    private func failed(_ connection: ConnectionAttempt, epoch token: UInt64, error: Error) {
        guard connection.active else { return }
        connection.cancel()
        guard current(token) else { return }
        lock.lock(); roots.removeAll(); lock.unlock()
        let terminal: Bool
        if let failure = error as? TransportFailure {
            switch failure {
            case .hostKey, .password, .invalidConfiguration, .invalidData: terminal = true
            default: terminal = false
            }
        } else if let ssl = error as? NIOSSLError {
            switch ssl {
            case .uncleanShutdown, .shutdownFailed: terminal = false
            case .handshakeFailed(.sslError(let errors)) where errors.contains(.eofDuringHandshake) || errors.contains(.eofDuringAdditionalCertficiateChainValidation): terminal = false
            case .handshakeFailed(.syscallError): terminal = false
            default: terminal = true
            }
        } else { terminal = error is NIOSSLExtraError || error is CommsProtocolError }
        let reason = (error as? TransportFailure)?.status ?? "SSH, TLS or protocol verification failed"
        if terminal {
            emitStatus("Disconnected: \(reason); correct setup and reconnect", epoch: token)
            lock.lock()
            if epoch == token { active = false; route = nil; credentials.clear(); epoch &+= 1 }
            lock.unlock()
            return
        }
        emitStatus("Disconnected: \(reason); retrying", epoch: token)
        guard current(token) else { return }
        let delay = retryDelay
        retryDelay = .nanoseconds(min(options.retryMaximum.nanoseconds, delay.nanoseconds * 2))
        retry = loop.scheduleTask(in: delay) { [weak self] in self?.connect(epoch: token) }
    }
}

final class ConnectionAttempt {
    var active = true
    private var roots: [Channel] = []
    var deadline: Scheduled<Void>?
    func register(_ channel: Channel) { roots.append(channel) }
    func cancel() {
        active = false; deadline?.cancel(); deadline = nil
        roots.forEach { $0.close(promise: nil) }; roots.removeAll()
    }
}

private final class ConnectionEnd: ChannelInboundHandler {
    typealias InboundIn = Any
    let failed: (Error) -> Void
    init(_ failed: @escaping (Error) -> Void) { self.failed = failed }
    func errorCaught(context: ChannelHandlerContext, error: Error) { failed(error); context.close(promise: nil) }
    func channelInactive(context: ChannelHandlerContext) { failed(TransportFailure.disconnected); context.fireChannelInactive() }
}
