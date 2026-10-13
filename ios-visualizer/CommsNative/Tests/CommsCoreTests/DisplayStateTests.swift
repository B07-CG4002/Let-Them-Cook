import Dispatch
import XCTest
@testable import CommsCore

final class DisplayStateTests: XCTestCase {
    private func result(seq: UInt32 = 42, boot: UInt32 = 7) throws -> GestureResult {
        let gestures = ["REST", "FIST", "OPEN", "POINT"]
        let body = "{\"v\":1,\"type\":\"GESTURE_RESULT\",\"session_id\":\"ltc-comms\",\"device_id\":1,\"boot_id\":\(boot),\"seq\":\(seq),\"result_id\":\"1:\(boot):\(seq)\",\"gesture\":\"\(gestures[Int(seq % 4)])\",\"confidence\":1}"
        return try CommsProtocol.result(Array(body.utf8), session: "ltc-comms")
    }

    func testOnlyNewestResultIsDisplayedAndExpiresAtTwoSeconds() throws {
        let state = DisplayState()
        let generation = state.begin()
        XCTAssertTrue(state.accept(try result(), generation: generation, now: 10))
        XCTAssertTrue(state.accept(try result(seq: 43), generation: generation, now: 10.5))
        let live = state.snapshot(now: 12.49)
        XCTAssertEqual(live.result?.resultID, "1:7:43")
        XCTAssertEqual(live.receivedCount, 2)
        let expired = state.snapshot(now: 12.5)
        XCTAssertNil(expired.result)
        XCTAssertGreaterThan(expired.revision, live.revision)
        XCTAssertEqual(state.snapshot(now: 12.6).revision, expired.revision)
        XCTAssertNil(state.snapshot(now: 10.6).result, "A backwards caller timestamp must not revive expired data")
    }

    func testDisconnectRetainsDedupAndDuplicateDoesNotRefreshFreshness() throws {
        let state = DisplayState()
        let generation = state.begin()
        let message = try result()
        XCTAssertTrue(state.accept(message, generation: generation, now: 10))
        XCTAssertFalse(state.accept(message, generation: generation, now: 11.9))
        XCTAssertNil(state.snapshot(now: 12).result)
        XCTAssertTrue(state.clear(generation: generation, status: "Disconnected"))
        XCTAssertFalse(state.accept(message, generation: generation, now: 13))
        XCTAssertTrue(state.accept(try result(seq: 43), generation: generation, now: 13))
        XCTAssertEqual(state.snapshot(now: 13).receivedCount, 2)
    }

    func testOldGenerationCannotAcceptClearOrChangeStatusAfterStopOrNewRun() throws {
        let state = DisplayState()
        let old = state.begin()
        XCTAssertTrue(state.accept(try result(), generation: old, now: 10))
        let stopped = state.stop(status: "Paused")
        XCTAssertGreaterThan(stopped, old)
        XCTAssertFalse(state.accept(try result(seq: 43), generation: old, now: 11))
        XCTAssertFalse(state.clear(generation: old, status: "Wrong"))
        XCTAssertFalse(state.setStatus("Wrong", generation: old))
        XCTAssertEqual(state.snapshot(now: 11).status, "Paused")
        XCTAssertNil(state.snapshot(now: 11).result)
        let next = state.begin(status: "Connecting")
        XCTAssertGreaterThan(next, stopped)
        XCTAssertTrue(state.accept(try result(), generation: next, now: 12), "Explicit new run resets dedup")
        XCTAssertEqual(state.snapshot(now: 12).receivedCount, 1)
    }

    func testStatusUpdatePreservesLiveResultAndClearRemovesIt() throws {
        let state = DisplayState()
        let generation = state.begin()
        XCTAssertTrue(state.accept(try result(), generation: generation, now: 1))
        XCTAssertTrue(state.setStatus("Subscribed", generation: generation))
        XCTAssertEqual(state.snapshot(now: 1).result?.gesture, "OPEN")
        XCTAssertTrue(state.clear(generation: generation, status: "Disconnected"))
        XCTAssertNil(state.snapshot(now: 1).result)
    }

    func testDedupEvictsOldestAfter4096UniqueResults() throws {
        let state = DisplayState()
        let generation = state.begin()
        for seq in UInt32(0)...4096 {
            XCTAssertTrue(state.accept(try result(seq: seq), generation: generation, now: 1))
        }
        XCTAssertFalse(state.accept(try result(seq: 1), generation: generation, now: 1))
        XCTAssertTrue(state.accept(try result(seq: 0), generation: generation, now: 1))
        XCTAssertEqual(state.snapshot(now: 1).receivedCount, 4098)
    }

    func testEarlierOrInvalidArrivalCannotReplaceNewerSnapshot() throws {
        let state = DisplayState()
        let generation = state.begin()
        XCTAssertTrue(state.accept(try result(seq: 43), generation: generation, now: 20))
        XCTAssertFalse(state.accept(try result(), generation: generation, now: 19))
        XCTAssertFalse(state.accept(try result(), generation: generation, now: .nan))
        XCTAssertFalse(state.accept(try result(), generation: generation, now: .infinity))
        XCTAssertEqual(state.snapshot(now: 20).result?.resultID, "1:7:43")
    }

    func testSnapshotPollingCannotDiscardFreshCallbackWhoseTimestampWasCapturedBeforeLock() throws {
        let state = DisplayState()
        let generation = state.begin()
        _ = state.snapshot(now: 10.001)
        XCTAssertTrue(state.accept(try result(), generation: generation, now: 10))
        XCTAssertEqual(state.snapshot(now: 10.002).result?.resultID, "1:7:42")
        XCTAssertFalse(state.accept(try result(seq: 41), generation: generation, now: 9.999))
    }

    func testDelayedCallbackCannotPublishAlreadyExpiredResult() throws {
        let state = DisplayState()
        let generation = state.begin()
        _ = state.snapshot(now: 20)
        XCTAssertFalse(state.accept(try result(), generation: generation, now: 18))
        XCTAssertTrue(state.accept(try result(), generation: generation, now: 18.5))
        XCTAssertNotNil(state.snapshot(now: 20).result)
        XCTAssertNil(state.snapshot(now: 20.5).result)
    }

    func testConcurrentDuplicateCallbacksCountOnce() throws {
        let state = DisplayState()
        let generation = state.begin()
        let message = try result()
        DispatchQueue.concurrentPerform(iterations: 1000) { _ in
            _ = state.accept(message, generation: generation, now: 1)
            _ = state.snapshot(now: 1)
        }
        XCTAssertEqual(state.snapshot(now: 1).receivedCount, 1)
        XCTAssertEqual(state.snapshot(now: 1).result?.resultID, "1:7:42")
    }
}
