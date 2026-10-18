import NIOCore
import NIOTLS
import CommsCore

/// This handler receives plaintext only after NIOSSL's verified handshake.
final class Subscriber: ChannelInboundHandler {
    typealias InboundIn = ByteBuffer
    private let session: String
    private let options: TransportOptions
    private let onSubscribed: () -> Void
    private let onResult: (GestureResult) -> Void
    private let onFailure: (Error) -> Void
    private var decoder = FrameDecoder()
    private var subscribed = false
    private var sentSubscribe = false
    private var partialFrame = false
    private var failed = false
    private var timer: Scheduled<Void>?

    init(session: String, options: TransportOptions, onSubscribed: @escaping () -> Void, onResult: @escaping (GestureResult) -> Void, onFailure: @escaping (Error) -> Void) {
        self.session = session; self.options = options
        self.onSubscribed = onSubscribed; self.onResult = onResult; self.onFailure = onFailure
    }
    func userInboundEventTriggered(context: ChannelHandlerContext, event: Any) {
        if case TLSUserEvent.handshakeCompleted = event {
            guard !sentSubscribe, !failed else { return }
            sentSubscribe = true
            do {
                let wire = try CommsProtocol.subscribe(session: session)
                var buffer = context.channel.allocator.buffer(capacity: wire.count); buffer.writeBytes(wire)
                context.writeAndFlush(NIOAny(buffer), promise: nil)
                arm(context, delay: options.frameTimeout)
            } catch { fail(context, error) }
        }
        context.fireUserInboundEventTriggered(event)
    }
    func channelRead(context: ChannelHandlerContext, data: NIOAny) {
        guard !failed, sentSubscribe else { fail(context, TransportFailure.invalidData); return }
        let bytes = Array(unwrapInboundIn(data).readableBytesView)
        guard !bytes.isEmpty else { return }
        // Clean idle has no deadline. The first byte of each result starts one
        // frame budget; later fragments of that frame never extend it.
        if !partialFrame && subscribed { arm(context, delay: options.frameTimeout) }
        do {
            let frames = try decoder.feed(bytes)
            for body in frames {
                if !subscribed {
                    try CommsProtocol.subscribed(body, session: session)
                    subscribed = true; onSubscribed()
                } else { onResult(try CommsProtocol.result(body, session: session)) }
            }
            partialFrame = decoder.hasPartialFrame
            if !partialFrame { disarm() }
            else if !frames.isEmpty && subscribed {
                // A previous frame completed and this same read began another.
                arm(context, delay: options.frameTimeout)
            }
        } catch { fail(context, error) }
    }
    private func arm(_ context: ChannelHandlerContext, delay: TimeAmount) {
        timer?.cancel()
        timer = context.eventLoop.scheduleTask(in: delay) { [weak self, weak context] in
            guard let self, let context else { return }
            self.fail(context, TransportFailure.frameDeadline)
        }
    }
    private func disarm() { timer?.cancel(); timer = nil }
    private func fail(_ context: ChannelHandlerContext, _ error: Error) {
        guard !failed else { return }; failed = true
        disarm()
        onFailure(error); context.close(promise: nil)
    }
    func errorCaught(context: ChannelHandlerContext, error: Error) { fail(context, error) }
    func channelInactive(context: ChannelHandlerContext) { fail(context, TransportFailure.disconnected); context.fireChannelInactive() }
    func handlerRemoved(context: ChannelHandlerContext) { disarm() }
}
