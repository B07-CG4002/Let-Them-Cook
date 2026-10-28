import NIOCore
import NIOTLS
import CommsCore

/// This handler receives plaintext only after NIOSSL's verified handshake.
final class Subscriber: ChannelInboundHandler {
    typealias InboundIn = ByteBuffer
    private let session: String
    private let handEvents: Bool
    private let options: TransportOptions
    private let onSubscribed: () -> Void
    private let onResult: (GestureResult) -> Void
    private let onHandEvent: (HandEvent) -> Void
    private let onInvalidHandEvent: () -> Void
    private let onUnacknowledged: () -> Void
    private let onFailure: (Error) -> Void
    private var decoder = FrameDecoder()
    private var subscribed = false
    private var sentSubscribe = false
    private var partialFrame = false
    private var failed = false
    private var timer: Scheduled<Void>?

    /// handEvents sends the version 2 SUBSCRIBE; without it this is the version 1
    /// subscriber, which treats every frame after SUBSCRIBED as a GESTURE_RESULT.
    /// onUnacknowledged runs before onFailure when a version 2 SUBSCRIBE went out on
    /// the verified connection and the connection ended without SUBSCRIBED.
    init(session: String, handEvents: Bool = false, options: TransportOptions, onSubscribed: @escaping () -> Void,
         onResult: @escaping (GestureResult) -> Void, onHandEvent: @escaping (HandEvent) -> Void = { _ in },
         onInvalidHandEvent: @escaping () -> Void = {}, onUnacknowledged: @escaping () -> Void = {}, onFailure: @escaping (Error) -> Void) {
        self.session = session; self.handEvents = handEvents; self.options = options
        self.onSubscribed = onSubscribed; self.onResult = onResult; self.onFailure = onFailure
        self.onHandEvent = onHandEvent; self.onInvalidHandEvent = onInvalidHandEvent; self.onUnacknowledged = onUnacknowledged
    }
    func userInboundEventTriggered(context: ChannelHandlerContext, event: Any) {
        if case TLSUserEvent.handshakeCompleted = event {
            guard !sentSubscribe, !failed else { return }
            sentSubscribe = true
            do {
                let wire = try CommsProtocol.subscribe(session: session, handEvents: handEvents)
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
                    try CommsProtocol.subscribed(body, session: session, handEvents: handEvents)
                    subscribed = true; onSubscribed()
                } else if !handEvents {
                    onResult(try CommsProtocol.result(body, session: session))
                } else {
                    // An invalid HAND_EVENT is counted and dropped; every other bad frame fails as on version 1.
                    switch try CommsProtocol.subscribedFrame(body, session: session) {
                    case .result(let result): onResult(result)
                    case .handEvent(let event): onHandEvent(event)
                    case .invalidHandEvent: onInvalidHandEvent()
                    }
                }
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
        if handEvents && sentSubscribe && !subscribed { onUnacknowledged() }
        onFailure(error); context.close(promise: nil)
    }
    func errorCaught(context: ChannelHandlerContext, error: Error) { fail(context, error) }
    func channelInactive(context: ChannelHandlerContext) { fail(context, TransportFailure.disconnected); context.fireChannelInactive() }
    func handlerRemoved(context: ChannelHandlerContext) { disarm() }
}
