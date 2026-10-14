import Dispatch
import Foundation
import XCTest
@testable import CommsCore

final class DisplayStateTests: XCTestCase {
    private func result(seq: UInt32 = 42, boot: UInt32 = 7, gesture: String = "CHOP", confidence: String = "0.87") throws -> GestureResult {
        let body = "{\"v\":3,\"type\":\"GESTURE_RESULT\",\"session_id\":\"ltc-comms\",\"device_id\":1,\"boot_id\":\(boot),\"seq\":\(seq),\"request_id\":null,\"result_id\":\"1:\(boot):\(seq)\",\"gesture\":\"\(gesture)\",\"confidence\":\(confidence)}"
        return try CommsProtocol.result(Array(body.utf8), session: "ltc-comms")
    }

    /// The compact JSON CommsNextResult would hand out next, or nil when nothing waits.
    private func nextJSON(_ state: DisplayState, now: Double) -> String? {
        guard case .taken(let bytes) = state.takeNext(capacity: 512, now: now) else { return nil }
        return String(decoding: bytes, as: UTF8.self)
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
        XCTAssertEqual(state.snapshot(now: 1).result?.gesture, "CHOP")
        XCTAssertEqual(state.snapshot(now: 1).result?.confidence, 0.87)
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
        XCTAssertEqual(state.snapshot(now: 1).pendingCount, 1)
    }

    // CommsNextResult queue.

    func testQueueHandsOutEachAcceptedResultOnceInArrivalOrder() throws {
        let state = DisplayState()
        let generation = state.begin()
        XCTAssertEqual(state.takeNext(capacity: 512, now: 1), .empty)
        let first = try result(seq: 1, gesture: "WASH", confidence: "1")
        let second = try result(seq: 2, gesture: "THROW", confidence: "0.5")
        XCTAssertTrue(state.accept(first, generation: generation, now: 1))
        XCTAssertTrue(state.accept(second, generation: generation, now: 1.1))
        XCTAssertFalse(state.accept(first, generation: generation, now: 1.2), "A duplicate result_id is not queued again")
        XCTAssertEqual(state.snapshot(now: 1.2).pendingCount, 2)
        XCTAssertEqual(nextJSON(state, now: 1.3), #"{"result_id":"1:7:1","gesture":"WASH","confidence":1.0,"device_id":1}"#)
        XCTAssertEqual(nextJSON(state, now: 1.3), #"{"result_id":"1:7:2","gesture":"THROW","confidence":0.5,"device_id":1}"#)
        XCTAssertEqual(state.takeNext(capacity: 512, now: 1.3), .empty)
        XCTAssertFalse(state.accept(second, generation: generation, now: 1.4), "A taken result is still de-duplicated")
        XCTAssertEqual(state.takeNext(capacity: 512, now: 1.4), .empty)
        let snapshot = state.snapshot(now: 1.4)
        XCTAssertEqual(snapshot.pendingCount, 0)
        XCTAssertEqual(snapshot.pendingDropped, 0)
        XCTAssertEqual(snapshot.pendingExpired, 0)
    }

    func testQueueKeepsAtMost32AndCountsEachDroppedOldest() throws {
        let state = DisplayState()
        let generation = state.begin()
        for seq in UInt32(0)..<40 {
            XCTAssertTrue(state.accept(try result(seq: seq), generation: generation, now: 1))
        }
        let full = state.snapshot(now: 1)
        XCTAssertEqual(full.pendingCount, 32)
        XCTAssertEqual(full.pendingDropped, 8)
        XCTAssertEqual(full.pendingExpired, 0)
        XCTAssertEqual(full.receivedCount, 40, "Drops affect only the queue, not the received count")
        for seq in UInt32(8)..<40 {
            XCTAssertEqual(nextJSON(state, now: 1), try result(seq: seq).nextResultJSON)
        }
        XCTAssertEqual(state.takeNext(capacity: 512, now: 1), .empty)
        XCTAssertEqual(state.snapshot(now: 1).pendingDropped, 8)
        _ = state.begin()
        XCTAssertEqual(state.snapshot(now: 1).pendingDropped, 0, "A new Connect starts new counters, like receivedCount")
    }

    func testQueuedResultExpiresTwoSecondsAfterArrival() throws {
        let state = DisplayState()
        let generation = state.begin()
        XCTAssertTrue(state.accept(try result(seq: 1), generation: generation, now: 10))
        XCTAssertTrue(state.accept(try result(seq: 2), generation: generation, now: 11))
        // At 12.5 the first has waited 2.5 s and expires; the second has waited 1.5 s.
        XCTAssertEqual(nextJSON(state, now: 12.5), try result(seq: 2).nextResultJSON)
        XCTAssertEqual(state.snapshot(now: 12.5).pendingExpired, 1)
        XCTAssertTrue(state.accept(try result(seq: 3), generation: generation, now: 12.5))
        XCTAssertTrue(state.accept(try result(seq: 4), generation: generation, now: 12.5))
        XCTAssertEqual(state.snapshot(now: 14.49).pendingCount, 2)
        XCTAssertEqual(nextJSON(state, now: 14.49), try result(seq: 3).nextResultJSON)
        XCTAssertEqual(state.takeNext(capacity: 512, now: 14.5), .empty, "Exactly 2 s after arrival is expired")
        XCTAssertEqual(state.snapshot(now: 14.5).pendingExpired, 2)
        XCTAssertTrue(state.accept(try result(seq: 5), generation: generation, now: 14.5))
        XCTAssertEqual(state.snapshot(now: 17).pendingExpired, 3, "The display poll also expires waiting results")
        XCTAssertEqual(state.takeNext(capacity: 512, now: 15), .empty, "An earlier caller timestamp cannot bring it back")
        XCTAssertEqual(state.snapshot(now: 17).pendingDropped, 0)
    }

    func testBeginAndStopEmptyTheQueueWhileReconnectStatusesKeepIt() throws {
        let state = DisplayState()
        let generation = state.begin()
        XCTAssertTrue(state.accept(try result(seq: 1), generation: generation, now: 1))
        XCTAssertTrue(state.clear(generation: generation, status: "Disconnected: connection lost; retrying"))
        XCTAssertTrue(state.setStatus("Connecting", generation: generation))
        XCTAssertTrue(state.setStatus("Subscribed", generation: generation))
        XCTAssertNil(state.snapshot(now: 1).result)
        XCTAssertEqual(state.snapshot(now: 1).pendingCount, 1, "Automatic reconnect keeps waiting results")
        XCTAssertEqual(nextJSON(state, now: 1), try result(seq: 1).nextResultJSON)

        XCTAssertTrue(state.accept(try result(seq: 2), generation: generation, now: 1))
        _ = state.stop(status: "Disconnected")
        XCTAssertEqual(state.takeNext(capacity: 512, now: 1), .empty, "Disconnect, app inactive and CommsStop empty the queue")
        let stopped = state.snapshot(now: 1)
        XCTAssertEqual(stopped.pendingCount, 0)
        XCTAssertEqual(stopped.pendingDropped, 0, "Emptying on stop is not counted as a loss")
        XCTAssertEqual(stopped.pendingExpired, 0)

        let restarted = state.begin()
        XCTAssertTrue(state.accept(try result(seq: 3), generation: restarted, now: 2))
        _ = state.begin()
        XCTAssertEqual(state.takeNext(capacity: 512, now: 2), .empty, "A new Connect empties the queue")
    }

    func testTakeNextLeavesAResultThatDoesNotFitQueued() throws {
        let state = DisplayState()
        let generation = state.begin()
        let message = try result()
        XCTAssertTrue(state.accept(message, generation: generation, now: 1))
        let bytes = Array(message.nextResultJSON.utf8)
        for capacity in [-1, 0, 1, bytes.count] {
            XCTAssertEqual(state.takeNext(capacity: capacity, now: 1), .needs(bytes.count + 1), "capacity \(capacity)")
        }
        XCTAssertEqual(state.snapshot(now: 1).pendingCount, 1)
        XCTAssertEqual(state.takeNext(capacity: bytes.count + 1, now: 1), .taken(bytes))
        XCTAssertEqual(state.takeNext(capacity: bytes.count + 1, now: 1), .empty)
    }

    func testConcurrentAcceptAndTakeHandOutEachResultAtMostOnce() throws {
        let state = DisplayState()
        let generation = state.begin()
        let messages = try (UInt32(0)..<1000).map { try result(seq: $0) }
        let collected = LockedStrings()
        DispatchQueue.concurrentPerform(iterations: 1000) { index in
            _ = state.accept(messages[index], generation: generation, now: 1)
            if case .taken(let bytes) = state.takeNext(capacity: 512, now: 1) {
                collected.append(String(decoding: bytes, as: UTF8.self))
            }
        }
        let taken = collected.get()
        let snapshot = state.snapshot(now: 1)
        XCTAssertEqual(Set(taken).count, taken.count)
        XCTAssertEqual(UInt64(taken.count) + snapshot.pendingDropped + UInt64(snapshot.pendingCount), 1000)
        XCTAssertEqual(snapshot.receivedCount, 1000)
    }
}

private final class LockedStrings: @unchecked Sendable {
    private let lock = NSLock()
    private var values: [String] = []
    func append(_ value: String) { lock.lock(); values.append(value); lock.unlock() }
    func get() -> [String] { lock.lock(); defer { lock.unlock() }; return values }
}
