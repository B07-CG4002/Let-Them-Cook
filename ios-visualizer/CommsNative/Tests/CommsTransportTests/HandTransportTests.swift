import XCTest
import Foundation
import NIOCore
import NIOEmbedded
import NIOTLS
import CommsCore
@testable import CommsTransport

final class HandTransportTests: XCTestCase {
    static var pki: TestPKI!
    override class func setUp() { super.setUp(); pki = try! TestPKI() }
    override class func tearDown() { pki = nil; super.tearDown() }

    private func frame(_ text: String, _ channel: EmbeddedChannel) -> ByteBuffer {
        var buffer = channel.allocator.buffer(capacity: text.utf8.count + 4)
        buffer.writeInteger(UInt32(text.utf8.count)); buffer.writeString(text)
        return buffer
    }

    /// A Subscriber on an embedded channel that has passed the TLS handshake. Returns
    /// the channel, the SUBSCRIBE bytes it wrote, and the log of its callbacks.
    private func subscriber(handEvents: Bool) throws -> (EmbeddedChannel, [UInt8], CallbackLog) {
        let channel = EmbeddedChannel(loop: EmbeddedEventLoop())
        let log = CallbackLog()
        let handler = Subscriber(session: "ltc-comms", handEvents: handEvents, options: TransportOptions(), onSubscribed: { log.add("subscribed") },
                                 onResult: { log.add("result \($0.resultID)") }, onHandEvent: { log.add("hand \($0.eventID)") },
                                 onInvalidHandEvent: { log.add("invalid") }, onUnacknowledged: { log.add("unacknowledged") },
                                 onFailure: { log.add("failure \($0)") })
        try channel.pipeline.syncOperations.addHandler(handler)
        channel.pipeline.fireUserInboundEventTriggered(TLSUserEvent.handshakeCompleted(negotiatedProtocol: nil))
        let written = try XCTUnwrap(try channel.readOutbound(as: ByteBuffer.self))
        return (channel, Array(written.readableBytesView), log)
    }

    func testVersion2SubscriberDeliversHandEventsBesideResultsAndDropsInvalidOnes() throws {
        let (channel, subscribe, log) = try subscriber(handEvents: true)
        XCTAssertEqual(subscribe, try CommsProtocol.subscribe(session: "ltc-comms", handEvents: true))
        _ = try channel.writeInbound(frame(LocalPeers.handAck, channel))
        for text in LocalPeers.handStream { _ = try channel.writeInbound(frame(text, channel)) }
        // One read carrying a malformed hand event and a result: the result still arrives.
        var coalesced = frame(#"{"v":4,"type":"HAND_EVENT","gesture":{}}"#, channel)
        var second = frame(LocalPeers.result, channel)
        coalesced.writeBuffer(&second)
        _ = try channel.writeInbound(coalesced)
        // A hand event whose strict parse stops before its type (here a non-flat v) is dropped too.
        var early = frame(#"{"v":[4],"type":"HAND_EVENT"}"#, channel)
        var third = frame(LocalPeers.result, channel)
        early.writeBuffer(&third)
        _ = try channel.writeInbound(early)
        let epoch = LocalPeers.epoch
        XCTAssertEqual(log.entries, ["subscribed", "result 1:7:42", "hand \(epoch):1:1", "invalid", "hand \(epoch):2:1", "result 1:7:42",
                                     "invalid", "result 1:7:42", "invalid", "result 1:7:42"])
        XCTAssertFalse(log.entries.contains { $0.hasPrefix("failure") }, "An invalid HAND_EVENT must not fail the subscription")
        // Any other type still fails the connection, as on version 1.
        _ = try? channel.writeInbound(frame(#"{"v":1,"type":"MONITOR_UPDATE","session_id":"ltc-comms"}"#, channel))
        XCTAssertEqual(log.entries.filter { $0.hasPrefix("failure") }.count, 1)
        XCTAssertFalse(log.entries.contains("unacknowledged"), "SUBSCRIBED had arrived")
        _ = try channel.finish(acceptAlreadyClosed: true)
    }

    func testVersion2SubscriberRefusesAVersion1Acknowledgement() throws {
        let (channel, _, log) = try subscriber(handEvents: true)
        _ = try? channel.writeInbound(frame(LocalPeers.ack, channel))
        XCTAssertEqual(log.entries.first, "unacknowledged")
        XCTAssertEqual(log.entries.filter { $0.hasPrefix("failure") }.count, 1)
        XCTAssertFalse(log.entries.contains("subscribed"))
        _ = try channel.finish(acceptAlreadyClosed: true)
    }

    func testVersion2SubscribeThatEndsWithoutSubscribedIsReportedBeforeTheFailure() throws {
        let (channel, _, log) = try subscriber(handEvents: true)
        channel.pipeline.fireChannelInactive()
        XCTAssertEqual(log.entries.count, 2)
        XCTAssertEqual(log.entries.first, "unacknowledged")
        XCTAssertTrue(log.entries.last?.hasPrefix("failure") == true)
        _ = try channel.finish(acceptAlreadyClosed: true)
        // Version 1 never reports it, and neither does a version 2 subscription that was acknowledged.
        let (old, _, oldLog) = try subscriber(handEvents: false)
        old.pipeline.fireChannelInactive()
        XCTAssertFalse(oldLog.entries.contains("unacknowledged"))
        _ = try old.finish(acceptAlreadyClosed: true)
        let (acknowledged, _, acknowledgedLog) = try subscriber(handEvents: true)
        _ = try acknowledged.writeInbound(frame(LocalPeers.handAck, acknowledged))
        acknowledged.pipeline.fireChannelInactive()
        XCTAssertFalse(acknowledgedLog.entries.contains("unacknowledged"))
        _ = try acknowledged.finish(acceptAlreadyClosed: true)
    }

    // An installed app subscribes with version 1 and the service never sends it a
    // HAND_EVENT; if one came anyway, it would be a protocol violation, as before.
    func testVersion1SubscriberIsUnchangedAndRefusesAHandEvent() throws {
        let (channel, subscribe, log) = try subscriber(handEvents: false)
        XCTAssertEqual(subscribe, [0, 0, 0, 51] + Array(#"{"v":1,"type":"SUBSCRIBE","session_id":"ltc-comms"}"#.utf8))
        _ = try channel.writeInbound(frame(LocalPeers.ack, channel))
        _ = try channel.writeInbound(frame(LocalPeers.result, channel))
        _ = try? channel.writeInbound(frame(LocalPeers.handStart, channel))
        XCTAssertEqual(log.entries.prefix(2), ["subscribed", "result 1:7:42"])
        XCTAssertEqual(log.entries.count, 3)
        XCTAssertTrue(log.entries.last?.hasPrefix("failure") == true)
        _ = try channel.finish(acceptAlreadyClosed: true)
    }

    func testVersion2SubscriptionDeliversResultsAndHandEventsThroughBothSSHHops() throws {
        let peers = try LocalPeers(pki: Self.pki, behavior: .handEvents)
        var options = peers.options; options.handEvents = true
        let done = expectation(description: "two results, two hand events, one invalid")
        done.expectedFulfillmentCount = 5
        let log = CallbackLog()
        let client = CommsClient(route: peers.route(), session: "ltc-comms", options: options, onStatus: { status in
            if status.hasPrefix("Disconnected") { log.add(status) }
        }, onResult: { log.add("result \($0.resultID)"); done.fulfill() },
           onHandEvent: { log.add("hand \($0.kind.rawValue) \($0.gesture ?? "")"); done.fulfill() },
           onInvalidHandEvent: { log.add("invalid"); done.fulfill() })
        client.start(); wait(for: [done], timeout: 4); client.stop()
        XCTAssertEqual(log.entries, ["result 1:7:42", "hand state_start CHOP", "invalid", "hand pulse THROW", "result 1:7:42"])
        XCTAssertEqual(peers.subscriptionCount, 1)
        XCTAssertEqual(peers.invalidSubscriptions, 0)
    }

    func testServiceWithoutHandEventsMakesTheClientUseVersion1ForTheRestOfItsLife() throws {
        XCTAssertTrue(TransportOptions().handEvents, "A new client subscribes with version 2 until a service shows it has no hand events")
        // The fixture closes on version 2, then also closes the first version 1 subscription.
        let peers = try LocalPeers(pki: Self.pki, behavior: .closeFirst)
        var options = peers.options; options.handEvents = true
        let got = expectation(description: "result after falling back to version 1")
        let statuses = CallbackLog()
        let client = CommsClient(route: peers.route(), session: "ltc-comms", options: options, onStatus: { statuses.add($0) },
                                 onResult: { _ in got.fulfill() }, onHandEvent: { _ in XCTFail("hand event on version 1") })
        client.start(); wait(for: [got], timeout: 4); client.stop()
        let disconnects = statuses.entries.filter { $0.hasPrefix("Disconnected") }
        // The reason part depends on how the TLS stream ended (close_notify or not).
        XCTAssertEqual(disconnects.count, 2, "\(disconnects)")
        XCTAssertTrue(disconnects.first?.hasSuffix("; no hand events from this service; retrying with subscription version 1") == true, "\(disconnects)")
        XCTAssertTrue(disconnects.last?.hasSuffix("; retrying") == true, "\(disconnects)")
        XCTAssertFalse(disconnects.last?.contains("version 1") == true, "\(disconnects)")
        XCTAssertEqual(peers.invalidSubscriptions, 1, "Only the first SUBSCRIBE was version 2")
        XCTAssertEqual(peers.subscriptionCount, 2)
    }
}

final class CallbackLog: @unchecked Sendable {
    private let lock = NSLock()
    private var list: [String] = []
    func add(_ entry: String) { lock.lock(); list.append(entry); lock.unlock() }
    var entries: [String] { lock.lock(); defer { lock.unlock() }; return list }
}
