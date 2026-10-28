import Foundation
import XCTest
import CommsCore
@testable import CommsBridge

final class HandMailboxTests: XCTestCase {
    private static let epoch = "0123456789abcdef0123456789abcdef"
    private static let idle = #"{"worker_epoch":null,"invalid":0,"hands":[{"device_id":1,"gesture":null,"state_id":null,"expires_in_ms":null,"last_pulse_gesture":null,"last_pulse_event_id":null,"accepted":0,"duplicate":0,"ignored":0},{"device_id":2,"gesture":null,"state_id":null,"expires_in_ms":null,"last_pulse_gesture":null,"last_pulse_event_id":null,"accepted":0,"duplicate":0,"ignored":0}]}"#

    // CommsBridgeTests sees CommsCore only through its public API.
    private func event(_ kind: String, device: Int = 1, seq: UInt64, gesture: String, session: String = "ltc-comms",
                       quotedSession: String? = nil) throws -> HandEvent {
        let epoch = Self.epoch
        let id = "\(epoch):\(device):\(seq)"
        let state = kind == "state_start" ? "\"\(id)\"" : "null"
        let lease = kind == "state_start" ? 1000 : 0
        let body = "{\"v\":4,\"type\":\"HAND_EVENT\",\"event_format\":1,\"session_id\":\"\(quotedSession ?? session)\",\"worker_epoch\":\"\(epoch)\","
            + "\"device_id\":\(device),\"event_seq\":\(seq),\"event_id\":\"\(id)\",\"kind\":\"\(kind)\",\"gesture\":\"\(gesture)\","
            + "\"confidence\":0.93,\"source\":{\"boot_id\":11,\"seq\":120,\"uptime_ms\":2400},\"state_id\":\(state),\"lease_ms\":\(lease),"
            + "\"reason\":\"confirmed\"}"
        return try CommsProtocol.handEvent(Array(body.utf8), session: session)
    }

    func testHandStateUsesTheNextResultReturnConvention() throws {
        let mailbox = HandMailbox(state: HandEventState())
        let count = Self.idle.utf8.count
        let needed = Int32(count + 1)
        var bytes = [CChar](repeating: 42, count: 2048)
        XCTAssertEqual(mailbox.handState(into: nil, capacity: 2048, now: 1), -needed)
        for capacity in [Int32(-1), 0, 1, needed - 1] {
            XCTAssertEqual(bytes.withUnsafeMutableBufferPointer { mailbox.handState(into: $0.baseAddress, capacity: capacity, now: 1) }, -needed)
            XCTAssertEqual(bytes, [CChar](repeating: 42, count: 2048), "capacity \(capacity) wrote into the buffer")
        }
        XCTAssertEqual(bytes.withUnsafeMutableBufferPointer { mailbox.handState(into: $0.baseAddress, capacity: needed, now: 1) }, Int32(count))
        XCTAssertEqual(bytes[count], 0)
        XCTAssertTrue(bytes[(count + 1)...].allSatisfy { $0 == 42 })
        XCTAssertEqual(String(cString: bytes), Self.idle)
        // The state is always there: a second call returns it again, never 0.
        XCTAssertEqual(bytes.withUnsafeMutableBufferPointer { mailbox.handState(into: $0.baseAddress, capacity: 2048, now: 1) }, Int32(count))
    }

    func testHandStateShowsTheLiveStateOfBothHands() throws {
        let state = HandEventState()
        let mailbox = HandMailbox(state: state)
        let generation = state.begin()
        _ = state.accept(try event("state_start", seq: 1, gesture: "CHOP"), generation: generation, now: 1)
        _ = state.accept(try event("pulse", device: 2, seq: 1, gesture: "THROW"), generation: generation, now: 1)
        var bytes = [CChar](repeating: 42, count: 2048)
        XCTAssertGreaterThan(bytes.withUnsafeMutableBufferPointer { mailbox.handState(into: $0.baseAddress, capacity: 2048, now: 1.5) }, 0)
        let object = try XCTUnwrap(JSONSerialization.jsonObject(with: Data(String(cString: bytes).utf8)) as? [String: Any])
        XCTAssertEqual(object["worker_epoch"] as? String, Self.epoch)
        let hands = try XCTUnwrap(object["hands"] as? [[String: Any]])
        XCTAssertEqual(hands.count, 2)
        XCTAssertEqual(hands[0]["gesture"] as? String, "CHOP")
        XCTAssertEqual(hands[0]["state_id"] as? String, "\(Self.epoch):1:1")
        XCTAssertEqual(hands[0]["expires_in_ms"] as? Int, 500)
        XCTAssertTrue(hands[1]["gesture"] is NSNull)
        XCTAssertEqual(hands[1]["last_pulse_gesture"] as? String, "THROW")
        XCTAssertEqual(hands[1]["last_pulse_event_id"] as? String, "\(Self.epoch):2:1")
    }

    func testNextHandEventHandsOutEachAcceptedEventOnceAndNeverOverrunsBuffer() throws {
        let state = HandEventState()
        let mailbox = HandMailbox(state: state)
        let generation = state.begin()
        var bytes = [CChar](repeating: 42, count: 2048)
        XCTAssertEqual(bytes.withUnsafeMutableBufferPointer { mailbox.nextHandEvent(into: $0.baseAddress, capacity: 2048, now: 1) }, 0)
        XCTAssertEqual(mailbox.nextHandEvent(into: nil, capacity: 0, now: 1), 0)
        let start = try event("state_start", seq: 1, gesture: "WASH")
        let pulse = try event("pulse", device: 2, seq: 1, gesture: "PICKUP")
        _ = state.accept(start, generation: generation, now: 1)
        _ = state.accept(start, generation: generation, now: 1)
        _ = state.accept(pulse, generation: generation, now: 1)
        let count = start.compactJSON.utf8.count
        let needed = Int32(count + 1)
        XCTAssertEqual(mailbox.nextHandEvent(into: nil, capacity: 2048, now: 1), -needed)
        for capacity in [Int32(-1), 0, 1, needed - 1] {
            XCTAssertEqual(bytes.withUnsafeMutableBufferPointer { mailbox.nextHandEvent(into: $0.baseAddress, capacity: capacity, now: 1) }, -needed)
            XCTAssertEqual(bytes, [CChar](repeating: 42, count: 2048), "capacity \(capacity) wrote into the buffer")
        }
        XCTAssertEqual(bytes.withUnsafeMutableBufferPointer { mailbox.nextHandEvent(into: $0.baseAddress, capacity: needed, now: 1) }, Int32(count))
        XCTAssertEqual(bytes[count], 0)
        XCTAssertTrue(bytes[(count + 1)...].allSatisfy { $0 == 42 })
        XCTAssertEqual(String(cString: bytes), start.compactJSON)
        let object = try XCTUnwrap(JSONSerialization.jsonObject(with: Data(String(cString: bytes).utf8)) as? [String: Any])
        XCTAssertEqual(object.count, 15)
        XCTAssertEqual(object["kind"] as? String, "state_start")
        XCTAssertEqual((object["source"] as? [String: Any])?["seq"] as? Int, 120)
        // The duplicate was not queued: the pulse is next, then nothing.
        XCTAssertGreaterThan(bytes.withUnsafeMutableBufferPointer { mailbox.nextHandEvent(into: $0.baseAddress, capacity: 2048, now: 1) }, 0)
        XCTAssertEqual(String(cString: bytes), pulse.compactJSON)
        XCTAssertEqual(bytes.withUnsafeMutableBufferPointer { mailbox.nextHandEvent(into: $0.baseAddress, capacity: 2048, now: 1) }, 0)
    }

    func testLargestEventsFitTheDocumentedBuffer() throws {
        // The longest event: a session of 128 four-byte scalars, or of 128 escaped quotes.
        // (HandStateTests checks the longest state.)
        let session = String(repeating: "\u{1F600}", count: 128)
        let largest = try event("state_start", seq: 9_007_199_254_740_991, gesture: "POURING", session: session)
        XCTAssertLessThan(largest.compactJSON.utf8.count, 1024)
        let quotes = String(repeating: "\"", count: 128)
        let quoted = try event("state_start", seq: 1, gesture: "WASH", session: quotes, quotedSession: String(repeating: "\\\"", count: 128))
        XCTAssertLessThan(quoted.compactJSON.utf8.count, 1024)
    }

    func testExportsAreLinkedAndAnswerBeforeAnySession() {
        var bytes = [CChar](repeating: 42, count: 2048)
        let count = bytes.withUnsafeMutableBufferPointer { commsHandState($0.baseAddress, Int32($0.count)) }
        XCTAssertEqual(count, Int32(Self.idle.utf8.count))
        XCTAssertEqual(String(cString: bytes), Self.idle)
        XCTAssertEqual(commsHandState(nil, 0), -(count + 1))
        XCTAssertEqual(bytes.withUnsafeMutableBufferPointer { commsNextHandEvent($0.baseAddress, Int32($0.count)) }, 0)
        XCTAssertEqual(commsNextHandEvent(nil, 0), 0)
        // CommsNextResult is untouched by the hand exports.
        XCTAssertEqual(bytes.withUnsafeMutableBufferPointer { commsNextResult($0.baseAddress, Int32($0.count)) }, 0)
    }
}
