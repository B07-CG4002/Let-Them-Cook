import XCTest
import CommsCore
@testable import CommsBridge

final class DisplayMailboxTests: XCTestCase {
    // CommsBridgeTests sees CommsCore only through its public API.
    private func result(seq: UInt32, gesture: String = "CHOP", confidence: String = "0.87") throws -> GestureResult {
        let body = "{\"v\":3,\"type\":\"GESTURE_RESULT\",\"session_id\":\"ltc-comms\",\"device_id\":1,\"boot_id\":7,\"seq\":\(seq),\"request_id\":null,\"result_id\":\"1:7:\(seq)\",\"gesture\":\"\(gesture)\",\"confidence\":\(confidence)}"
        return try CommsProtocol.result(Array(body.utf8), session: "ltc-comms")
    }

    func testCopiesOnlyChangedDisplayAndNeverOverrunsBuffer() throws {
        let state = DisplayState()
        let mailbox = DisplayMailbox(state: state)
        var bytes = [CChar](repeating: 42, count: 2048)
        let first = bytes.withUnsafeMutableBufferPointer { mailbox.copy(into: $0.baseAddress, capacity: Int32($0.count)) }
        XCTAssertGreaterThan(first, 0)
        XCTAssertEqual(bytes[Int(first)], 0)
        XCTAssertEqual(bytes[Int(first) + 1], 42)
        XCTAssertTrue(String(cString: bytes).contains("Communications"))
        XCTAssertEqual(bytes.withUnsafeMutableBufferPointer { mailbox.copy(into: $0.baseAddress, capacity: Int32($0.count)) }, 0)
        let generation = state.begin(status: "Connecting")
        var tiny = [CChar](repeating: 42, count: 2)
        XCTAssertEqual(tiny.withUnsafeMutableBufferPointer { mailbox.copy(into: $0.baseAddress, capacity: 2) }, 0)
        XCTAssertEqual(tiny, [42, 42])
        XCTAssertGreaterThan(bytes.withUnsafeMutableBufferPointer { mailbox.copy(into: $0.baseAddress, capacity: Int32($0.count)) }, 0)
        XCTAssertTrue(String(cString: bytes).contains("Connecting"))
        _ = state.clear(generation: generation, status: "Disconnected")
        XCTAssertGreaterThan(bytes.withUnsafeMutableBufferPointer { mailbox.copy(into: $0.baseAddress, capacity: Int32($0.count)) }, 0)
        XCTAssertTrue(String(cString: bytes).contains("Disconnected"))
        XCTAssertTrue(String(cString: bytes).contains("No live result"))
    }

    func testDisplayShowsTitleStatusAndReceivedConfidence() throws {
        let state = DisplayState()
        let mailbox = DisplayMailbox(state: state)
        let generation = state.begin(status: "Subscribed")
        XCTAssertTrue(state.accept(try result(seq: 1), generation: generation))
        var bytes = [CChar](repeating: 42, count: 2048)
        XCTAssertGreaterThan(bytes.withUnsafeMutableBufferPointer { mailbox.copy(into: $0.baseAddress, capacity: Int32($0.count)) }, 0)
        XCTAssertEqual(String(cString: bytes), "Communications\nSubscribed\nCHOP  |  1:7:1  |  confidence 0.87\nReceived: 1")
    }

    func testNextResultHandsOutCompactJSONOnceInOrderAndNeverOverrunsBuffer() throws {
        let state = DisplayState()
        let mailbox = DisplayMailbox(state: state)
        let generation = state.begin(status: "Subscribed")
        var bytes = [CChar](repeating: 42, count: 512)
        XCTAssertEqual(bytes.withUnsafeMutableBufferPointer { mailbox.nextResult(into: $0.baseAddress, capacity: Int32($0.count), now: 1) }, 0)
        XCTAssertEqual(bytes, [CChar](repeating: 42, count: 512), "Nothing waiting writes nothing")

        XCTAssertTrue(state.accept(try result(seq: 1), generation: generation, now: 1))
        XCTAssertTrue(state.accept(try result(seq: 2, gesture: "WASH", confidence: "1"), generation: generation, now: 1))
        let expected = #"{"result_id":"1:7:1","gesture":"CHOP","confidence":0.87,"device_id":1}"#
        let count = expected.utf8.count
        let needed = Int32(count + 1)

        // A null buffer or a buffer without room for the terminating 0 writes nothing.
        XCTAssertEqual(mailbox.nextResult(into: nil, capacity: 512, now: 1), -needed)
        XCTAssertEqual(mailbox.nextResult(into: nil, capacity: 0, now: 1), -needed)
        for capacity in [Int32(-1), 0, 1, needed - 1] {
            XCTAssertEqual(bytes.withUnsafeMutableBufferPointer { mailbox.nextResult(into: $0.baseAddress, capacity: capacity, now: 1) }, -needed, "capacity \(capacity)")
            XCTAssertEqual(bytes, [CChar](repeating: 42, count: 512), "capacity \(capacity) wrote into the buffer")
        }
        XCTAssertEqual(state.snapshot(now: 1).pendingCount, 2, "A result that did not fit stays queued")

        // Exactly bytes + 1 is enough; nothing past the terminating 0 changes.
        XCTAssertEqual(bytes.withUnsafeMutableBufferPointer { mailbox.nextResult(into: $0.baseAddress, capacity: needed, now: 1) }, Int32(count))
        XCTAssertEqual(bytes[count], 0)
        XCTAssertTrue(bytes[(count + 1)...].allSatisfy { $0 == 42 })
        XCTAssertEqual(String(cString: bytes), expected)

        bytes = [CChar](repeating: 42, count: 512)
        let second = bytes.withUnsafeMutableBufferPointer { mailbox.nextResult(into: $0.baseAddress, capacity: Int32($0.count), now: 1) }
        XCTAssertGreaterThan(second, 0)
        XCTAssertEqual(bytes[Int(second)], 0)
        XCTAssertEqual(bytes[Int(second) + 1], 42)
        XCTAssertEqual(String(cString: bytes), #"{"result_id":"1:7:2","gesture":"WASH","confidence":1.0,"device_id":1}"#)
        XCTAssertEqual(bytes.withUnsafeMutableBufferPointer { mailbox.nextResult(into: $0.baseAddress, capacity: Int32($0.count), now: 1) }, 0)
        XCTAssertEqual(mailbox.nextResult(into: nil, capacity: 0, now: 1), 0)
    }

    func testNextResultIsEmptyAfterStopAndExpiresAfterTwoSeconds() throws {
        let state = DisplayState()
        let mailbox = DisplayMailbox(state: state)
        var bytes = [CChar](repeating: 42, count: 512)
        var generation = state.begin(status: "Subscribed")
        XCTAssertTrue(state.accept(try result(seq: 1), generation: generation, now: 1))
        _ = state.stop(status: "Stopped")
        XCTAssertEqual(bytes.withUnsafeMutableBufferPointer { mailbox.nextResult(into: $0.baseAddress, capacity: Int32($0.count), now: 1) }, 0)

        generation = state.begin(status: "Subscribed")
        XCTAssertTrue(state.accept(try result(seq: 2), generation: generation, now: 5))
        XCTAssertEqual(bytes.withUnsafeMutableBufferPointer { mailbox.nextResult(into: $0.baseAddress, capacity: Int32($0.count), now: 7) }, 0)
        XCTAssertEqual(bytes, [CChar](repeating: 42, count: 512))
        XCTAssertEqual(state.snapshot(now: 7).pendingExpired, 1)
    }
}
