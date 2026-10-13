import Foundation
import NIOCore
import NIOPosix
import NIOSSH
import NIOSSL
import Crypto
import CommsCore
@testable import CommsTransport

// Every secret here is generated for loopback-only tests. macOS creates fresh
// PKI directly; iOS copies a fresh authority from the host-generated test bundle.
// Both paths delete their private temporary copy at teardown.
final class TestPKI {
    let directory: URL
    let ca: String
    let certificate: NIOSSLCertificate
    let key: NIOSSLPrivateKey
    init(hostname: String = "ultra96.week7.internal", expired: Bool = false) throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        self.directory = directory
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
        do {
            #if os(iOS)
            let fixture = try IOSFixturePool.shared.take(hostname: hostname, expired: expired)
            for filename in ["ca.pem", "server.pem", "server.key"] {
                let source = fixture.appendingPathComponent(filename)
                let destination = directory.appendingPathComponent(filename)
                guard try source.resourceValues(forKeys: [.isRegularFileKey, .isSymbolicLinkKey]).isRegularFile == true,
                      try source.resourceValues(forKeys: [.isSymbolicLinkKey]).isSymbolicLink != true else { throw FixtureError.pki }
                try FileManager.default.copyItem(at: source, to: destination)
                try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: destination.path)
            }
            #else
            let config = """
            [req]
            distinguished_name=dn
            prompt=no
            [dn]
            CN=Comms isolated test authority
            [ca]
            basicConstraints=critical,CA:TRUE
            keyUsage=critical,keyCertSign,cRLSign
            [server]
            basicConstraints=critical,CA:FALSE
            keyUsage=critical,digitalSignature,keyEncipherment
            extendedKeyUsage=serverAuth
            subjectAltName=DNS:\(hostname)
            [issuer]
            database=index
            serial=serial
            private_key=ca.key
            certificate=ca.pem
            new_certs_dir=.
            default_md=sha256
            policy=subject_policy
            [subject_policy]
            commonName=supplied
            """
            try config.write(to: directory.appendingPathComponent("config"), atomically: true, encoding: .utf8)
            func run(_ args: [String]) throws {
                let process = Process()
                process.executableURL = URL(fileURLWithPath: "/usr/bin/openssl")
                process.arguments = args
                process.currentDirectoryURL = directory
                process.standardOutput = FileHandle.nullDevice
                process.standardError = FileHandle.nullDevice
                try process.run()
                process.waitUntilExit()
                guard process.terminationStatus == 0 else { throw FixtureError.pki }
            }
            try run(["req", "-new", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", "ca.key", "-out", "ca.pem", "-days", "1", "-config", "config", "-extensions", "ca"])
            try run(["req", "-new", "-newkey", "rsa:2048", "-nodes", "-keyout", "server.key", "-out", "server.csr", "-config", "config", "-subj", "/CN=\(hostname)"])
            if expired {
                try "".write(to: directory.appendingPathComponent("index"), atomically: true, encoding: .utf8)
                try "01\n".write(to: directory.appendingPathComponent("serial"), atomically: true, encoding: .utf8)
                try run(["ca", "-batch", "-config", "config", "-name", "issuer", "-in", "server.csr", "-out", "server.pem", "-extensions", "server", "-startdate", "20000101000000Z", "-enddate", "20000102000000Z"])
            } else {
                try run(["x509", "-req", "-in", "server.csr", "-CA", "ca.pem", "-CAkey", "ca.key", "-CAcreateserial", "-out", "server.pem", "-days", "1", "-extfile", "config", "-extensions", "server"])
            }
            #endif
            ca = try String(contentsOf: directory.appendingPathComponent("ca.pem"))
            certificate = try NIOSSLCertificate.fromPEMFile(directory.appendingPathComponent("server.pem").path)[0]
            key = try NIOSSLPrivateKey(file: directory.appendingPathComponent("server.key").path, format: .pem)
        } catch {
            try? FileManager.default.removeItem(at: directory)
            throw error
        }
    }
    deinit { try? FileManager.default.removeItem(at: directory) }
}

#if os(iOS)
private final class FixtureBundleAnchor: NSObject {}

/// Tests that construct an unrelated CA must never accidentally reuse an
/// authority. Exhaustion and unsupported fixture variants fail closed.
private final class IOSFixturePool: @unchecked Sendable {
    static let shared = IOSFixturePool()
    private let lock = NSLock()
    private var consumed: [String: Int] = [:]

    func take(hostname: String, expired: Bool) throws -> URL {
        let kind: String
        let capacity: Int
        switch (hostname, expired) {
        case ("ultra96.week7.internal", false): kind = "valid"; capacity = 8
        case ("wrong.week7.internal", false): kind = "wrong-host"; capacity = 2
        case ("ultra96.week7.internal", true): kind = "expired"; capacity = 2
        default: throw FixtureError.pki
        }
        lock.lock(); defer { lock.unlock() }
        let index = consumed[kind, default: 0]
        guard index < capacity,
              let root = Bundle(for: FixtureBundleAnchor.self).url(forResource: "CommsFixturePKI", withExtension: nil) else {
            throw FixtureError.pki
        }
        consumed[kind] = index + 1
        let fixture = root.appendingPathComponent(String(format: "%@-%02d", kind, index), isDirectory: true)
        let values = try fixture.resourceValues(forKeys: [.isDirectoryKey, .isSymbolicLinkKey])
        guard values.isDirectory == true, values.isSymbolicLink != true else { throw FixtureError.pki }
        return fixture
    }
}
#endif

enum FixtureError: Error { case pki, forbiddenTarget, incorrectSubscription }

final class FixtureAuth: NIOSSHServerUserAuthenticationDelegate {
    var supportedAuthenticationMethods: NIOSSHAvailableUserAuthenticationMethods { .password }
    func requestReceived(request: NIOSSHUserAuthenticationRequest, responsePromise: EventLoopPromise<NIOSSHUserAuthenticationOutcome>) {
        if case .password(let password) = request.request, request.username == "fixture", password.password == "fixture-only-password" {
            responsePromise.succeed(.success)
        } else { responsePromise.succeed(.failure) }
    }
}

final class Relay: ChannelInboundHandler {
    typealias InboundIn = ByteBuffer
    let peer: Channel
    init(_ peer: Channel) { self.peer = peer }
    func channelRead(context: ChannelHandlerContext, data: NIOAny) { peer.writeAndFlush(unwrapInboundIn(data), promise: nil) }
    func channelInactive(context: ChannelHandlerContext) { peer.close(promise: nil); context.fireChannelInactive() }
    func errorCaught(context: ChannelHandlerContext, error: Error) { peer.close(promise: nil); context.close(promise: nil) }
}

final class FixtureConnections {
    private let lock = NSLock()
    private var channels: [Channel] = []
    func add(_ channel: Channel) { lock.lock(); channels.append(channel); lock.unlock() }
    func close() { lock.lock(); let list = channels; channels.removeAll(); lock.unlock(); list.forEach { $0.close(promise: nil) } }
    var activeCount: Int { lock.lock(); defer { lock.unlock() }; return channels.filter(\.isActive).count }
}

final class LocalPeers {
    enum Behavior { case good, idle, partialPrefix, partialBody, resultBeforeAck, malformedResult, closeFirst }
    static let result = #"{"v":1,"type":"GESTURE_RESULT","session_id":"ltc-comms","device_id":1,"boot_id":7,"seq":42,"result_id":"1:7:42","gesture":"OPEN","confidence":1.0}"#
    static let ack = #"{"v":1,"type":"SUBSCRIBED","session_id":"ltc-comms"}"#
    let group = MultiThreadedEventLoopGroup(numberOfThreads: 1)
    let boardKey = NIOSSHPrivateKey(ed25519Key: Curve25519.Signing.PrivateKey())
    let jumpKey = NIOSSHPrivateKey(ed25519Key: Curve25519.Signing.PrivateKey())
    let connections = FixtureConnections()
    let pki: TestPKI
    private(set) var tls: Channel!
    private(set) var board: Channel!
    private(set) var jump: Channel!
    private let lock = NSLock()
    private var subscriptions = 0
    private var failures = 0
    private var servicePort = 0
    private let stallSSH: Bool
    private let onSSHConnection: (() -> Void)?
    var subscriptionCount: Int { lock.lock(); defer { lock.unlock() }; return subscriptions }
    var invalidSubscriptions: Int { lock.lock(); defer { lock.unlock() }; return failures }

    init(pki: TestPKI, behavior: Behavior = .good, externalServicePort: Int? = nil, stallSSH: Bool = false, onSSHConnection: (() -> Void)? = nil, rejectFirstForward: Bool = false) throws {
        self.pki = pki; self.stallSSH = stallSSH; self.onSSHConnection = onSSHConnection
        if externalServicePort == nil {
        let context = try NIOSSLContext(configuration: .makeServerConfiguration(certificateChain: [.certificate(pki.certificate)], privateKey: .privateKey(pki.key)))
        tls = try ServerBootstrap(group: group).childChannelInitializer { [weak self] channel in
            guard let self else { return channel.eventLoop.makeFailedFuture(FixtureError.forbiddenTarget) }
            self.connections.add(channel)
            return channel.eventLoop.makeCompletedFuture {
                try channel.pipeline.syncOperations.addHandlers(NIOSSLServerHandler(context: context), FixtureProtocol(behavior: behavior) { [weak self] valid in
                guard let self else { return 0 }
                self.lock.lock(); defer { self.lock.unlock() }
                if valid { self.subscriptions += 1 } else { self.failures += 1 }
                return self.subscriptions
                })
            }
        }.bind(host: "127.0.0.1", port: 0).wait()
        }
        servicePort = externalServicePort ?? tls.localAddress!.port!
        board = try sshServer(key: boardKey, targetPort: servicePort, rejectFirst: rejectFirstForward)
        jump = try sshServer(key: jumpKey, targetPort: board.localAddress!.port!)
    }

    private func sshServer(key: NIOSSHPrivateKey, targetPort: Int, rejectFirst: Bool = false) throws -> Channel {
        var rejectNext = rejectFirst
        return try ServerBootstrap(group: group).childChannelInitializer { [weak self] channel in
            guard let self else { return channel.eventLoop.makeFailedFuture(FixtureError.forbiddenTarget) }
            self.connections.add(channel)
            self.onSSHConnection?()
            if self.stallSSH { return channel.eventLoop.makeSucceededFuture(()) }
            let ssh = NIOSSHHandler(role: .server(.init(hostKeys: [key], userAuthDelegate: FixtureAuth())), allocator: channel.allocator) { [weak self] child, type in
                guard let self else { return child.eventLoop.makeFailedFuture(FixtureError.forbiddenTarget) }
                guard case .directTCPIP(let target) = type, target.targetHost == "127.0.0.1", target.targetPort == targetPort else {
                    return child.eventLoop.makeFailedFuture(FixtureError.forbiddenTarget)
                }
                if rejectNext { rejectNext = false; return child.eventLoop.makeFailedFuture(FixtureError.forbiddenTarget) }
                return ClientBootstrap(group: child.eventLoop)
                    .channelOption(ChannelOptions.autoRead, value: false)
                    .channelInitializer { outbound in
                        self.connections.add(outbound)
                        return outbound.pipeline.addHandler(Relay(child))
                    }.connect(host: "127.0.0.1", port: targetPort).flatMap { outbound in
                        child.pipeline.addHandlers(FixtureSSHBytes(), Relay(outbound)).flatMap { outbound.setOption(ChannelOptions.autoRead, value: true) }
                    }
            }
            return channel.eventLoop.makeCompletedFuture { try channel.pipeline.syncOperations.addHandler(ssh) }
        }.bind(host: "127.0.0.1", port: 0).wait()
    }

    func route(twoHop: Bool = true, ca: String? = nil, badBoardKey: Bool = false, badJumpKey: Bool = false, password: String = "fixture-only-password") -> SSHRoute {
        SSHRoute(boardHost: "127.0.0.1", boardUser: "fixture", boardPassword: password,
                 boardHostKey: String(openSSHPublicKey: badBoardKey ? jumpKey.publicKey : boardKey.publicKey),
                 jump: twoHop ? .init(host: "127.0.0.1", user: "fixture", password: "fixture-only-password", hostKey: String(openSSHPublicKey: badJumpKey ? boardKey.publicKey : jumpKey.publicKey)) : nil,
                 caPEM: ca ?? pki.ca)
    }
    var options: TransportOptions {
        var options = TransportOptions()
        options.boardPort = board.localAddress!.port!
        options.jumpPort = jump.localAddress!.port!
        options.servicePort = servicePort
        options.connectTimeout = .seconds(3)
        options.frameTimeout = .milliseconds(180)
        options.retryMinimum = .milliseconds(50)
        options.retryMaximum = .milliseconds(100)
        return options
    }
    func closeConnections() { connections.close() }
    deinit {
        connections.close()
        try? jump?.close().wait()
        try? board?.close().wait()
        try? tls?.close().wait()
        try? group.syncShutdownGracefully()
    }
}

final class FixtureProtocol: ChannelInboundHandler {
    typealias InboundIn = ByteBuffer
    var bytes: [UInt8] = []
    let behavior: LocalPeers.Behavior
    let subscribe: (Bool) -> Int
    init(behavior: LocalPeers.Behavior, subscribe: @escaping (Bool) -> Int) { self.behavior = behavior; self.subscribe = subscribe }
    func channelRead(context: ChannelHandlerContext, data: NIOAny) {
        bytes.append(contentsOf: unwrapInboundIn(data).readableBytesView)
        guard bytes.count >= 4 else { return }
        let length = bytes.prefix(4).reduce(0) { ($0 << 8) | Int($1) }
        guard bytes.count >= length + 4 else { return }
        let body = Array(bytes[4..<(length + 4)])
        bytes.removeFirst(length + 4)
        let value = (try? JSONSerialization.jsonObject(with: Data(body))) as? [String: Any]
        let valid = value?.count == 3 && value?["type"] as? String == "SUBSCRIBE" && value?["session_id"] as? String == "ltc-comms" && value?["v"] as? Int == 1
        let count = subscribe(valid)
        guard valid else { context.close(promise: nil); return }
        if behavior == .closeFirst && count == 1 { context.close(promise: nil); return }
        if behavior == .resultBeforeAck { send(LocalPeers.result, context); return }
        send(LocalPeers.ack, context)
        switch behavior {
        case .good, .closeFirst: send(LocalPeers.result, context)
        case .idle: break
        case .partialPrefix:
            var buffer = context.channel.allocator.buffer(capacity: 1); buffer.writeInteger(UInt8(0)); context.writeAndFlush(NIOAny(buffer), promise: nil)
        case .partialBody:
            var buffer = context.channel.allocator.buffer(capacity: 6); buffer.writeInteger(UInt32(50)); buffer.writeString("{\""); context.writeAndFlush(NIOAny(buffer), promise: nil)
        case .malformedResult: send(#"{"v":1}"#, context)
        case .resultBeforeAck: break
        }
    }
    private func send(_ text: String, _ context: ChannelHandlerContext) {
        var buffer = context.channel.allocator.buffer(capacity: text.utf8.count + 4)
        buffer.writeInteger(UInt32(text.utf8.count)); buffer.writeString(text)
        context.writeAndFlush(NIOAny(buffer), promise: nil)
    }
    func errorCaught(context: ChannelHandlerContext, error: Error) { context.close(promise: nil) }
}

final class FixtureSSHBytes: ChannelDuplexHandler {
    typealias InboundIn = SSHChannelData
    typealias InboundOut = ByteBuffer
    typealias OutboundIn = ByteBuffer
    typealias OutboundOut = SSHChannelData
    func channelRead(context: ChannelHandlerContext, data: NIOAny) {
        guard case .byteBuffer(let bytes) = unwrapInboundIn(data).data else { context.close(promise: nil); return }
        context.fireChannelRead(wrapInboundOut(bytes))
    }
    func write(context: ChannelHandlerContext, data: NIOAny, promise: EventLoopPromise<Void>?) {
        context.write(wrapOutboundOut(SSHChannelData(type: .channel, data: .byteBuffer(unwrapOutboundIn(data)))), promise: promise)
    }
}

final class DualStackResolver: Resolver, @unchecked Sendable {
    let loop: EventLoop
    init(loop: EventLoop) { self.loop = loop }
    func initiateAQuery(host: String, port: Int) -> EventLoopFuture<[SocketAddress]> {
        loop.makeSucceededFuture([try! SocketAddress(ipAddress: "127.0.0.1", port: port)])
    }
    func initiateAAAAQuery(host: String, port: Int) -> EventLoopFuture<[SocketAddress]> {
        loop.makeSucceededFuture([try! SocketAddress(ipAddress: "::1", port: port)])
    }
    func cancelQueries() {}
}
