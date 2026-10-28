import Foundation
import XCTest
@testable import CommsCore

final class HandStateTests: XCTestCase {
    private let epoch = HandFixture.epoch

    private func event(_ kind: String, device: Int = 1, seq: UInt64, gesture: String? = nil, state: UInt64 = 1, lease: Int? = nil,
                       reason: String? = nil, epoch: String = HandFixture.epoch, source: String? = HandFixture.source) throws -> HandEvent {
        try HandFixture.event(HandFixture.body(kind, device: device, seq: seq, gesture: gesture, source: source, state: state, lease: lease,
                                               reason: reason, epoch: epoch))
    }

    private func hand(_ tracker: inout HandStateTracker, _ device: Int, now: Double) -> HandSnapshot {
        tracker.snapshot(now: now)[device - 1]
    }

    func testHandsAreIndependent() throws {
        var tracker = HandStateTracker()
        XCTAssertEqual(tracker.apply(try event("state_start", device: 1, seq: 1, gesture: "CHOP"), now: 1), .stateStarted)
        XCTAssertEqual(tracker.apply(try event("state_start", device: 2, seq: 1, gesture: "WASH"), now: 1), .stateStarted)
        XCTAssertEqual(hand(&tracker, 1, now: 1).gesture, "CHOP")
        XCTAssertEqual(hand(&tracker, 2, now: 1).gesture, "WASH")
        // Equal event_seq values on different hands are different events.
        XCTAssertEqual(tracker.apply(try event("pulse", device: 1, seq: 2, gesture: "PICKUP"), now: 1.2), .pulse)
        let left = hand(&tracker, 1, now: 1.2)
        XCTAssertNil(left.gesture)
        XCTAssertNil(left.stateID)
        XCTAssertNil(left.expiresInMS)
        XCTAssertEqual(left.lastPulseGesture, "PICKUP")
        XCTAssertEqual(left.lastPulseEventID, "\(epoch):1:2")
        XCTAssertEqual(tracker.lastEnd(device: 1), HandEnd(stateID: "\(epoch):1:1", gesture: "CHOP", reason: "confirmed_transition"))
        let right = hand(&tracker, 2, now: 1.2)
        XCTAssertEqual(right.gesture, "WASH")
        XCTAssertEqual(right.stateID, "\(epoch):2:1")
        XCTAssertEqual(right.expiresInMS, 800)
        XCTAssertNil(right.lastPulseGesture)
        XCTAssertNil(tracker.lastEnd(device: 2))
        XCTAssertEqual([left.deviceID, right.deviceID], [1, 2])
        XCTAssertEqual([left.accepted, right.accepted], [2, 1])
    }

    func testDuplicatesAndOlderEventsChangeNothing() throws {
        var tracker = HandStateTracker()
        let start = try event("state_start", seq: 1)
        let refresh = try event("state_refresh", seq: 2, state: 1)
        XCTAssertEqual(tracker.apply(start, now: 0), .stateStarted)
        XCTAssertEqual(tracker.apply(start, now: 0.1), .duplicate)
        XCTAssertEqual(tracker.apply(refresh, now: 0.5), .refreshed)
        // A retransmitted refresh does not extend the lease.
        XCTAssertEqual(tracker.apply(refresh, now: 1.2), .duplicate)
        XCTAssertEqual(tracker.apply(start, now: 1.3), .outOfOrder)
        XCTAssertEqual(hand(&tracker, 1, now: 1.49).expiresInMS, 10)
        XCTAssertNil(hand(&tracker, 1, now: 1.5).gesture)
        // A later pulse is accepted; replaying the same pulse is not a second request.
        let pulse = try event("pulse", seq: 3)
        XCTAssertEqual(tracker.apply(pulse, now: 2), .pulse)
        XCTAssertEqual(tracker.apply(pulse, now: 2), .duplicate)
        let counts = hand(&tracker, 1, now: 2)
        XCTAssertEqual([counts.accepted, counts.duplicate, counts.ignored], [3, 3, 1])
    }

    func testNewWorkerEpochEndsBothStatesAndRestartsCounters() throws {
        var tracker = HandStateTracker()
        _ = tracker.apply(try event("state_start", device: 1, seq: 5), now: 1)
        _ = tracker.apply(try event("state_start", device: 2, seq: 8, gesture: "POURING"), now: 1)
        XCTAssertEqual(tracker.workerEpoch, epoch)
        // The worker reconnected: its counters start again at 1, on the right hand first.
        XCTAssertEqual(tracker.apply(try event("pulse", device: 2, seq: 1, gesture: "THROW", epoch: HandFixture.otherEpoch), now: 1.1), .pulse)
        XCTAssertEqual(tracker.workerEpoch, HandFixture.otherEpoch)
        XCTAssertEqual(tracker.lastEnd(device: 1)?.reason, "connection_reset")
        XCTAssertEqual(tracker.lastEnd(device: 2)?.reason, "connection_reset")
        XCTAssertNil(hand(&tracker, 1, now: 1.1).gesture)
        XCTAssertNil(hand(&tracker, 2, now: 1.1).gesture)
        XCTAssertEqual(tracker.apply(try event("state_start", device: 1, seq: 1, epoch: HandFixture.otherEpoch), now: 1.2), .stateStarted)
        XCTAssertEqual(hand(&tracker, 1, now: 1.2).stateID, "\(HandFixture.otherEpoch):1:1")
    }

    func testLeaseExpiresOnTheInjectedClockAndARefreshCannotReviveIt() throws {
        var tracker = HandStateTracker()
        _ = tracker.apply(try event("state_start", seq: 1, lease: 1000), now: 10)
        XCTAssertEqual(hand(&tracker, 1, now: 10.25).expiresInMS, 750)
        XCTAssertEqual(hand(&tracker, 1, now: 10.999).gesture, "CHOP")
        XCTAssertNil(hand(&tracker, 1, now: 11).gesture)
        XCTAssertEqual(tracker.lastEnd(device: 1), HandEnd(stateID: "\(epoch):1:1", gesture: "CHOP", reason: "lease_expired"))
        XCTAssertNil(hand(&tracker, 1, now: 10.5).gesture, "An earlier caller time must not revive an expired state")
        XCTAssertEqual(tracker.apply(try event("state_refresh", seq: 2, state: 1), now: 11.2), .orphanRefresh)
        XCTAssertNil(hand(&tracker, 1, now: 11.2).gesture)
        // A refresh within the lease moves the deadline to its own time plus its lease.
        _ = tracker.apply(try event("state_start", seq: 3, gesture: "WASH", lease: 500), now: 20)
        XCTAssertEqual(tracker.apply(try event("state_refresh", seq: 4, gesture: "WASH", state: 3, lease: 500), now: 20.4), .refreshed)
        XCTAssertEqual(hand(&tracker, 1, now: 20.85).expiresInMS, 50)
        XCTAssertNil(hand(&tracker, 1, now: 20.9).gesture)
        // Expiry also happens on the next event, before it is applied.
        _ = tracker.apply(try event("state_start", seq: 5, lease: 100), now: 30)
        XCTAssertEqual(tracker.apply(try event("state_end", seq: 6, state: 5), now: 30.2), .orphanEnd)
        XCTAssertEqual(tracker.lastEnd(device: 1)?.reason, "lease_expired")
    }

    func testOrphanRefreshesAndEndsAreIgnored() throws {
        var tracker = HandStateTracker()
        XCTAssertEqual(tracker.apply(try event("state_refresh", seq: 1, state: 1), now: 1), .orphanRefresh)
        XCTAssertEqual(tracker.apply(try event("state_end", seq: 2, state: 1), now: 1), .orphanEnd)
        XCTAssertNil(hand(&tracker, 1, now: 1).gesture, "A refresh never creates a state")
        _ = tracker.apply(try event("state_start", seq: 3, gesture: "CHOP"), now: 1)
        _ = tracker.apply(try event("state_start", seq: 4, gesture: "WASH"), now: 1.1)
        XCTAssertEqual(tracker.lastEnd(device: 1)?.stateID, "\(epoch):1:3")
        // The old state's end and refresh, and a refresh naming the wrong gesture, leave WASH alone.
        XCTAssertEqual(tracker.apply(try event("state_end", seq: 5, gesture: "CHOP", state: 3), now: 1.2), .orphanEnd)
        XCTAssertEqual(tracker.apply(try event("state_refresh", seq: 6, gesture: "CHOP", state: 3), now: 1.2), .orphanRefresh)
        XCTAssertEqual(tracker.apply(try event("state_refresh", seq: 7, gesture: "CHOP", state: 4), now: 1.2), .orphanRefresh)
        let snapshot = hand(&tracker, 1, now: 1.2)
        XCTAssertEqual(snapshot.gesture, "WASH")
        XCTAssertEqual(snapshot.stateID, "\(epoch):1:4")
        XCTAssertEqual(snapshot.expiresInMS, 900, "Orphans do not extend the lease")
        XCTAssertEqual([snapshot.accepted, snapshot.duplicate, snapshot.ignored], [2, 0, 5])
        // The matching end clears it, with the end's own reason.
        XCTAssertEqual(tracker.apply(try event("state_end", seq: 8, gesture: "WASH", state: 4, reason: "prediction_timeout"), now: 1.3), .stateEnded)
        XCTAssertEqual(tracker.lastEnd(device: 1), HandEnd(stateID: "\(epoch):1:4", gesture: "WASH", reason: "prediction_timeout"))
    }

    func testTransitionPairOnOneSourceSample() throws {
        var tracker = HandStateTracker()
        _ = tracker.apply(try event("state_start", seq: 1, gesture: "CHOP"), now: 1)
        XCTAssertEqual(tracker.apply(try event("state_end", seq: 2, gesture: "CHOP", state: 1, reason: "confirmed_transition"), now: 1.5), .stateEnded)
        XCTAssertEqual(tracker.apply(try event("state_start", seq: 3, gesture: "POURING"), now: 1.5), .stateStarted)
        XCTAssertEqual(tracker.lastEnd(device: 1), HandEnd(stateID: "\(epoch):1:1", gesture: "CHOP", reason: "confirmed_transition"))
        let snapshot = hand(&tracker, 1, now: 1.5)
        XCTAssertEqual(snapshot.gesture, "POURING")
        XCTAssertEqual(snapshot.stateID, "\(epoch):1:3")
        XCTAssertEqual(snapshot.accepted, 3)
    }

    func testIdleAndResetClearTheHand() throws {
        var tracker = HandStateTracker()
        _ = tracker.apply(try event("state_start", seq: 1), now: 1)
        XCTAssertEqual(tracker.apply(try event("idle", seq: 2), now: 1.1), .idle)
        XCTAssertEqual(tracker.lastEnd(device: 1)?.reason, "confirmed_idle")
        _ = tracker.apply(try event("state_start", seq: 3, gesture: "WASH"), now: 1.2)
        XCTAssertEqual(tracker.apply(try event("reset", seq: 4, reason: "source_reset", source: nil), now: 1.3), .reset)
        XCTAssertEqual(tracker.lastEnd(device: 1), HandEnd(stateID: "\(epoch):1:3", gesture: "WASH", reason: "source_reset"))
        XCTAssertNil(hand(&tracker, 1, now: 1.3).gesture)
    }

    func testOwnResubscriptionForgetsStatesButKeepsCounts() throws {
        var tracker = HandStateTracker()
        let start = try event("state_start", seq: 1)
        _ = tracker.apply(start, now: 1)
        _ = tracker.apply(try event("pulse", device: 2, seq: 1), now: 1)
        tracker.reset()
        XCTAssertNil(tracker.workerEpoch)
        XCTAssertEqual(tracker.lastEnd(device: 1)?.reason, "subscription_reset")
        let snapshot = tracker.snapshot(now: 1)
        XCTAssertNil(snapshot[0].gesture)
        XCTAssertNil(snapshot[1].lastPulseGesture)
        XCTAssertEqual(snapshot.map(\.accepted), [1, 1])
        XCTAssertEqual(tracker.apply(start, now: 1.1), .stateStarted, "The counters start again after a new subscription")
    }

    // HandEventState: the lock, the generations, the CommsNextHandEvent queue and the JSON.

    private func next(_ state: HandEventState, now: Double) -> String? {
        guard case .taken(let bytes) = state.takeNext(capacity: 2048, now: now) else { return nil }
        return String(decoding: bytes, as: UTF8.self)
    }

    func testQueueHandsOutEachAcceptedEventOnceInOrder() throws {
        let state = HandEventState()
        let generation = state.begin()
        let start = try event("state_start", seq: 1)
        let pulse = try event("pulse", device: 2, seq: 1)
        XCTAssertEqual(state.accept(start, generation: generation, now: 1), .stateStarted)
        XCTAssertEqual(state.accept(start, generation: generation, now: 1), .duplicate)
        XCTAssertEqual(state.accept(try event("state_refresh", seq: 2, state: 2), generation: generation, now: 1), .orphanRefresh)
        XCTAssertEqual(state.accept(pulse, generation: generation, now: 1), .pulse)
        let needed = start.compactJSON.utf8.count + 1
        XCTAssertEqual(state.takeNext(capacity: needed - 1, now: 1), .needs(needed))
        XCTAssertEqual(state.snapshot(now: 1).pendingCount, 2, "An event that did not fit stays queued")
        XCTAssertEqual(next(state, now: 1), start.compactJSON)
        XCTAssertEqual(next(state, now: 1), pulse.compactJSON)
        XCTAssertNil(next(state, now: 1))
    }

    func testQueueIsBoundedAndExpires() throws {
        let state = HandEventState()
        let generation = state.begin()
        for seq in 1...UInt64(HandEventState.pendingCapacity + 1) {
            XCTAssertEqual(state.accept(try event("pulse", seq: seq), generation: generation, now: 1), .pulse)
        }
        var snapshot = state.snapshot(now: 1)
        XCTAssertEqual(snapshot.pendingCount, HandEventState.pendingCapacity)
        XCTAssertEqual(snapshot.pendingDropped, 1)
        XCTAssertEqual(next(state, now: 1), try event("pulse", seq: 2).compactJSON, "The oldest event was dropped")
        XCTAssertNil(next(state, now: 3), "Events expire two seconds after arrival")
        snapshot = state.snapshot(now: 3)
        XCTAssertEqual(snapshot.pendingExpired, UInt64(HandEventState.pendingCapacity - 1))
        XCTAssertEqual(snapshot.pendingCount, 0)
    }

    func testGenerationsResubscriptionAndInvalidCount() throws {
        let state = HandEventState()
        let old = state.begin()
        XCTAssertEqual(state.accept(try event("state_start", seq: 1), generation: old, now: 1), .stateStarted)
        XCTAssertTrue(state.rejectInvalid(generation: old))
        XCTAssertEqual(state.snapshot(now: 1).invalid, 1)
        // A new subscription ends the states and restarts the counters; waiting events stay.
        XCTAssertTrue(state.resubscribe(generation: old))
        var snapshot = state.snapshot(now: 1)
        XCTAssertNil(snapshot.workerEpoch)
        XCTAssertNil(snapshot.hands[0].gesture)
        XCTAssertEqual(snapshot.pendingCount, 1)
        XCTAssertEqual(state.accept(try event("state_start", seq: 1), generation: old, now: 1.1), .stateStarted)
        let stopped = state.stop()
        XCTAssertGreaterThan(stopped, old)
        XCTAssertNil(state.accept(try event("pulse", seq: 2), generation: old, now: 1.2))
        XCTAssertFalse(state.rejectInvalid(generation: old))
        XCTAssertFalse(state.resubscribe(generation: old))
        XCTAssertNil(next(state, now: 1.2), "Stop discards waiting events")
        XCTAssertNil(state.snapshot(now: 1.2).hands[0].gesture)
        let fresh = state.begin()
        XCTAssertGreaterThan(fresh, stopped)
        snapshot = state.snapshot(now: 1.2)
        XCTAssertEqual(snapshot.invalid, 0)
        XCTAssertEqual(snapshot.hands.map(\.accepted), [0, 0])
        XCTAssertEqual(state.accept(try event("pulse", seq: 2), generation: fresh, now: 1.3), .pulse)
    }

    func testStateJSONHasBothHandsInOrder() throws {
        let state = HandEventState()
        XCTAssertEqual(state.snapshot(now: 0).compactJSON,
                       #"{"worker_epoch":null,"invalid":0,"hands":[{"device_id":1,"gesture":null,"state_id":null,"expires_in_ms":null,"last_pulse_gesture":null,"last_pulse_event_id":null,"accepted":0,"duplicate":0,"ignored":0},{"device_id":2,"gesture":null,"state_id":null,"expires_in_ms":null,"last_pulse_gesture":null,"last_pulse_event_id":null,"accepted":0,"duplicate":0,"ignored":0}]}"#)
        let generation = state.begin()
        _ = state.accept(try event("state_start", seq: 1, gesture: "POURING"), generation: generation, now: 1)
        _ = state.accept(try event("pulse", device: 2, seq: 3, gesture: "DROP"), generation: generation, now: 1)
        _ = state.accept(try event("pulse", device: 2, seq: 3, gesture: "DROP"), generation: generation, now: 1)
        _ = state.rejectInvalid(generation: generation)
        let json = state.snapshot(now: 1.25).compactJSON
        XCTAssertEqual(json, "{\"worker_epoch\":\"\(epoch)\",\"invalid\":1,\"hands\":["
            + "{\"device_id\":1,\"gesture\":\"POURING\",\"state_id\":\"\(epoch):1:1\",\"expires_in_ms\":750,\"last_pulse_gesture\":null,"
            + "\"last_pulse_event_id\":null,\"accepted\":1,\"duplicate\":0,\"ignored\":0},"
            + "{\"device_id\":2,\"gesture\":null,\"state_id\":null,\"expires_in_ms\":null,\"last_pulse_gesture\":\"DROP\","
            + "\"last_pulse_event_id\":\"\(epoch):2:3\",\"accepted\":1,\"duplicate\":1,\"ignored\":0}]}")
        let object = try XCTUnwrap(JSONSerialization.jsonObject(with: Data(json.utf8)) as? [String: Any])
        XCTAssertEqual((object["hands"] as? [Any])?.count, 2)
    }

    func testLongestStateJSONFitsTheDocumentedBuffer() {
        // Every string at its longest and every count at its largest (HandMailbox says under 1,024 bytes).
        let longest = HandSnapshot(deviceID: 1, gesture: "POURING", stateID: "\(epoch):1:9007199254740991", expiresInMS: 2000,
                                   lastPulseGesture: "PICKUP", lastPulseEventID: "\(epoch):1:9007199254740991",
                                   accepted: .max, duplicate: .max, ignored: .max)
        let snapshot = HandStateSnapshot(workerEpoch: epoch, invalid: .max, hands: [longest, longest],
                                         pendingCount: 0, pendingDropped: 0, pendingExpired: 0)
        XCTAssertLessThan(snapshot.compactJSON.utf8.count, 1024)
        XCTAssertNotNil(try? JSONSerialization.jsonObject(with: Data(snapshot.compactJSON.utf8)))
    }
}
