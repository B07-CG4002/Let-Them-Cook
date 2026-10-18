import NIOCore
import NIOSSH

final class PinnedHostKey: NIOSSHClientServerAuthenticationDelegate {
    private let pinned: NIOSSHPublicKey
    init(_ key: String) throws { pinned = try NIOSSHPublicKey(openSSHPublicKey: key) }
    func validateHostKey(hostKey: NIOSSHPublicKey, validationCompletePromise: EventLoopPromise<Void>) {
        guard hostKey == pinned else { validationCompletePromise.fail(TransportFailure.hostKey); return }
        validationCompletePromise.succeed(())
    }
}

final class PasswordAuthentication: NIOSSHClientUserAuthenticationDelegate {
    private let username: String
    private let credentials: CredentialVault
    private let jumpHop: Bool
    private var offered = false
    init(username: String, credentials: CredentialVault, jumpHop: Bool) {
        self.username = username; self.credentials = credentials; self.jumpHop = jumpHop
    }
    func nextAuthenticationType(availableMethods: NIOSSHAvailableUserAuthenticationMethods, nextChallengePromise: EventLoopPromise<NIOSSHUserAuthenticationOffer?>) {
        func fail(_ reason: PasswordFailureReason) {
            nextChallengePromise.fail(TransportFailure.password(hop: jumpHop ? .jump : .board, reason: reason))
        }
        // NIOSSH initially supplies .all; the server's actual methods arrive after
        // an unsuccessful offer. Do not mistake an unavailable method for a bad password.
        guard availableMethods.contains(.password) else { fail(.methodUnavailable); return }
        guard !offered else { fail(.rejected); return }
        guard let password = credentials.password(jumpHop: jumpHop) else { fail(.credentialsUnavailable); return }
        guard !password.isEmpty else { fail(.empty); return }
        offered = true
        nextChallengePromise.succeed(.init(username: username, serviceName: "", offer: .password(.init(password: password))))
    }
}

/// Converts only ordinary direct-tcpip channel bytes; extended data is a protocol error.
final class SSHByteStream: ChannelDuplexHandler {
    typealias InboundIn = SSHChannelData
    typealias InboundOut = ByteBuffer
    typealias OutboundIn = ByteBuffer
    typealias OutboundOut = SSHChannelData
    func channelRead(context: ChannelHandlerContext, data: NIOAny) {
        let packet = unwrapInboundIn(data)
        guard packet.type == .channel, case .byteBuffer(let bytes) = packet.data else {
            context.fireErrorCaught(TransportFailure.invalidData); return
        }
        context.fireChannelRead(wrapInboundOut(bytes))
    }
    func write(context: ChannelHandlerContext, data: NIOAny, promise: EventLoopPromise<Void>?) {
        context.write(wrapOutboundOut(SSHChannelData(type: .channel, data: .byteBuffer(unwrapOutboundIn(data)))), promise: promise)
    }
}
