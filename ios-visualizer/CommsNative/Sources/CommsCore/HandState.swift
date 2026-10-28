import Foundation

/// What the tracker did with one HAND_EVENT. The names are those of Wen Ree's
/// EventConsumer.apply (src/hand_events.py).
public enum HandOutcome: String, Equatable, Sendable {
    case pulse, refreshed, idle, reset
    case stateStarted = "state_started"
    case stateEnded = "state_ended"
    case duplicate
    case outOfOrder = "out_of_order"
    case orphanRefresh = "orphan_refresh"
    case orphanEnd = "orphan_end"

    /// A pulse, or an event that changed the hand's state. Only these reach
    /// CommsNextHandEvent; the others are counted and dropped.
    public var accepted: Bool {
        switch self {
        case .duplicate, .outOfOrder, .orphanRefresh, .orphanEnd: return false
        default: return true
        }
    }
}

/// How a hand's ongoing state ended: the end or reset event's own reason;
/// "confirmed_transition" when a pulse or a new start replaced it; "confirmed_idle";
/// "lease_expired"; "connection_reset" when a new worker_epoch arrived; or
/// "subscription_reset" when this phone subscribed again.
public struct HandEnd: Equatable, Sendable {
    public let stateID: String
    public let gesture: String
    public let reason: String
}

/// One hand's view, as CommsHandState hands it to the game.
public struct HandSnapshot: Equatable, Sendable {
    public let deviceID: UInt32
    /// The ongoing gesture (CHOP, WASH or POURING) and its state_id, or nil.
    public let gesture: String?
    public let stateID: String?
    /// Milliseconds until the state's lease runs out on this phone's clock (rounded to
    /// the nearest, so 0 while under half a millisecond remains), or nil.
    public let expiresInMS: Int?
    public let lastPulseGesture: String?
    public let lastPulseEventID: String?
    /// Events that were applied, repeated event_seq values, and the rest (older
    /// event_seq values, refreshes and ends of a state that is not active).
    public let accepted: UInt64
    public let duplicate: UInt64
    public let ignored: UInt64
}

/// Per-hand state from the HAND_EVENT stream, kept the way Wen Ree's EventConsumer
/// keeps it, on this phone's own monotonic clock (`now`, in seconds). Each hand has
/// its own high-water event_seq within the current worker_epoch: an equal event_seq
/// is a duplicate and a lower one is out of order, and neither changes anything. An
/// event of a new worker_epoch ends both hands' states and starts fresh counters. A
/// refresh never creates or revives a state, and an old state's end never clears a
/// newer one. A state that sees no matching refresh within its lease ends by itself.
/// Not thread-safe: HandEventState keeps it under its lock.
public struct HandStateTracker {
    private struct Active {
        let stateID: String
        let gesture: String
        var deadline: Double
    }
    private struct Hand {
        var highWater: UInt64 = 0
        var active: Active?
        var lastPulse: (gesture: String, eventID: String)?
        var lastEnd: HandEnd?
        var accepted: UInt64 = 0
        var duplicate: UInt64 = 0
        var ignored: UInt64 = 0
    }
    public private(set) var workerEpoch: String?
    private var hands = [Hand(), Hand()]
    // The latest time seen. An earlier caller time never revives or extends a state.
    private var clock: Double = 0

    public init() {}

    public mutating func apply(_ event: HandEvent, now: Double) -> HandOutcome {
        expire(now: now)
        // The parser admits only device 1 or 2.
        let index = Int(event.deviceID) - 1
        guard hands.indices.contains(index) else { return .outOfOrder }
        if workerEpoch != event.workerEpoch {
            // A new worker connection: its counters start again at 1.
            if workerEpoch != nil {
                for index in hands.indices { end(index, reason: "connection_reset"); hands[index].highWater = 0 }
            }
            workerEpoch = event.workerEpoch
        }
        if event.eventSeq == hands[index].highWater { hands[index].duplicate += 1; return .duplicate }
        if event.eventSeq < hands[index].highWater { hands[index].ignored += 1; return .outOfOrder }
        hands[index].highWater = event.eventSeq
        let outcome: HandOutcome
        switch event.kind {
        case .pulse:
            end(index, reason: "confirmed_transition")
            hands[index].lastPulse = (gesture: event.gesture ?? "", eventID: event.eventID)
            outcome = .pulse
        case .stateStart:
            end(index, reason: "confirmed_transition")
            hands[index].active = Active(stateID: event.eventID, gesture: event.gesture ?? "", deadline: clock + Double(event.leaseMS) / 1000)
            outcome = .stateStarted
        case .stateRefresh:
            if let active = hands[index].active, active.stateID == event.stateID, active.gesture == event.gesture {
                hands[index].active?.deadline = clock + Double(event.leaseMS) / 1000
                outcome = .refreshed
            } else { outcome = .orphanRefresh }
        case .stateEnd:
            if let active = hands[index].active, active.stateID == event.stateID {
                end(index, reason: event.reason)
                outcome = .stateEnded
            } else { outcome = .orphanEnd }
        case .idle:
            end(index, reason: "confirmed_idle")
            outcome = .idle
        case .reset:
            end(index, reason: event.reason)
            outcome = .reset
        }
        if outcome.accepted { hands[index].accepted += 1 } else { hands[index].ignored += 1 }
        return outcome
    }

    /// Ends every state whose lease ran out by `now` (reason lease_expired).
    public mutating func expire(now: Double) {
        if now.isFinite { clock = max(clock, now) }
        for index in hands.indices {
            if let active = hands[index].active, clock >= active.deadline { end(index, reason: "lease_expired") }
        }
    }

    /// A new subscription of this phone. The service does not replay events, so the
    /// states, the last pulses, the worker_epoch and its counters start again; the
    /// accepted, duplicate and ignored counts are kept.
    public mutating func reset(reason: String = "subscription_reset") {
        for index in hands.indices {
            end(index, reason: reason)
            hands[index].highWater = 0
            hands[index].lastPulse = nil
        }
        workerEpoch = nil
    }

    public mutating func snapshot(now: Double) -> [HandSnapshot] {
        expire(now: now)
        return hands.indices.map { index in
            let hand = hands[index]
            return HandSnapshot(deviceID: UInt32(index + 1), gesture: hand.active?.gesture, stateID: hand.active?.stateID,
                                expiresInMS: hand.active.map { Int((($0.deadline - clock) * 1000).rounded()) },
                                lastPulseGesture: hand.lastPulse?.gesture, lastPulseEventID: hand.lastPulse?.eventID,
                                accepted: hand.accepted, duplicate: hand.duplicate, ignored: hand.ignored)
        }
    }

    /// The last state of that hand (1 or 2) that ended, and why; nil if none has.
    public func lastEnd(device: UInt32) -> HandEnd? {
        let index = Int(device) - 1
        return hands.indices.contains(index) ? hands[index].lastEnd : nil
    }

    private mutating func end(_ index: Int, reason: String) {
        guard let active = hands[index].active else { return }
        hands[index].lastEnd = HandEnd(stateID: active.stateID, gesture: active.gesture, reason: reason)
        hands[index].active = nil
    }
}

/// The hand tracker and the CommsNextHandEvent queue, shared like DisplayState:
/// networking callbacks write while the Unity main thread polls. All state is behind
/// one lock; no timers or callbacks run under it.
public final class HandEventState: @unchecked Sendable {
    /// Accepted events waiting for CommsNextHandEvent, and seconds from arrival.
    public static let pendingCapacity = 64
    public static let pendingLifetime: Double = 2
    private let lock = NSLock()
    private var generation: UInt64 = 0
    private var active = false
    private var tracker = HandStateTracker()
    private var invalid: UInt64 = 0
    private var observedTime: Double = 0
    // Accepted events in arrival order, each with its arrival time.
    private var pending: [(event: HandEvent, arrivedAt: Double)] = []
    private var pendingDropped: UInt64 = 0
    private var pendingExpired: UInt64 = 0

    public init() {}

    /// Begins an explicit user session: everything starts again.
    public func begin() -> UInt64 {
        lock.lock()
        defer { lock.unlock() }
        generation += 1
        active = true
        tracker = HandStateTracker()
        invalid = 0
        pending.removeAll(keepingCapacity: true)
        pendingDropped = 0
        pendingExpired = 0
        return generation
    }

    /// Ends the session: states end and waiting events are discarded without being counted as lost.
    public func stop() -> UInt64 {
        lock.lock()
        defer { lock.unlock() }
        generation += 1
        active = false
        tracker.reset(reason: "stopped")
        pending.removeAll(keepingCapacity: true)
        return generation
    }

    /// Each connection step of the client (Connecting, Subscribed, Disconnected) ends the
    /// previous subscription's hand states. Waiting events are kept; they still expire.
    @discardableResult
    public func resubscribe(generation: UInt64) -> Bool {
        lock.lock()
        defer { lock.unlock() }
        guard active, generation == self.generation else { return false }
        tracker.reset()
        return true
    }

    /// Applies a validated HAND_EVENT; an accepted one also waits for CommsNextHandEvent.
    /// Returns nil when the generation is not current.
    @discardableResult
    public func accept(_ event: HandEvent, generation: UInt64, now: Double = ProcessInfo.processInfo.systemUptime) -> HandOutcome? {
        lock.lock()
        defer { lock.unlock() }
        guard active, generation == self.generation else { return nil }
        if now.isFinite { observedTime = max(observedTime, now) }
        let outcome = tracker.apply(event, now: observedTime)
        expirePending()
        if outcome.accepted {
            if pending.count == HandEventState.pendingCapacity {
                pending.removeFirst()
                pendingDropped += 1
            }
            pending.append((event: event, arrivedAt: observedTime))
        }
        return outcome
    }

    /// Counts a HAND_EVENT that failed validation and was dropped.
    @discardableResult
    public func rejectInvalid(generation: UInt64) -> Bool {
        lock.lock()
        defer { lock.unlock() }
        guard active, generation == self.generation else { return false }
        invalid += 1
        return true
    }

    public func snapshot(now: Double = ProcessInfo.processInfo.systemUptime) -> HandStateSnapshot {
        lock.lock()
        defer { lock.unlock() }
        if now.isFinite { observedTime = max(observedTime, now) }
        expirePending()
        return HandStateSnapshot(workerEpoch: tracker.workerEpoch, invalid: invalid, hands: tracker.snapshot(now: observedTime),
                                 pendingCount: pending.count, pendingDropped: pendingDropped, pendingExpired: pendingExpired)
    }

    /// Drops expired events, then takes the oldest waiting event if its compact JSON and
    /// a terminating 0 fit in `capacity` bytes; otherwise it stays queued.
    public func takeNext(capacity: Int, now: Double = ProcessInfo.processInfo.systemUptime) -> NextResult {
        lock.lock()
        defer { lock.unlock() }
        if now.isFinite { observedTime = max(observedTime, now) }
        expirePending()
        guard let oldest = pending.first else { return .empty }
        let bytes = Array(oldest.event.compactJSON.utf8)
        guard capacity > bytes.count else { return .needs(bytes.count + 1) }
        pending.removeFirst()
        return .taken(bytes)
    }

    // Call with the lock held. Arrival times never decrease, so expired events are at the front.
    private func expirePending() {
        while let oldest = pending.first, observedTime - oldest.arrivedAt >= HandEventState.pendingLifetime {
            pending.removeFirst()
            pendingExpired += 1
        }
    }
}

/// Both hands at one moment, plus the stream's counts.
public struct HandStateSnapshot: Equatable, Sendable {
    public let workerEpoch: String?
    /// HAND_EVENT frames that failed validation and were dropped.
    public let invalid: UInt64
    /// The LEFT hand (device 1), then the RIGHT hand (device 2).
    public let hands: [HandSnapshot]
    public let pendingCount: Int
    public let pendingDropped: UInt64
    public let pendingExpired: UInt64
}

extension HandStateSnapshot {
    /// The object CommsHandState hands to the game, keys in this order. Every string in
    /// it is a worker_epoch, an event_id, a state_id or a gesture name, which the parser
    /// limits to ASCII letters, digits and colons, so none needs escaping.
    public var compactJSON: String {
        func text(_ value: String?) -> String { value.map { "\"\($0)\"" } ?? "null" }
        let hands = self.hands.map { hand in
            "{\"device_id\":\(hand.deviceID),\"gesture\":\(text(hand.gesture)),\"state_id\":\(text(hand.stateID)),"
                + "\"expires_in_ms\":\(hand.expiresInMS.map { "\($0)" } ?? "null"),"
                + "\"last_pulse_gesture\":\(text(hand.lastPulseGesture)),\"last_pulse_event_id\":\(text(hand.lastPulseEventID)),"
                + "\"accepted\":\(hand.accepted),\"duplicate\":\(hand.duplicate),\"ignored\":\(hand.ignored)}"
        }
        return "{\"worker_epoch\":\(text(workerEpoch)),\"invalid\":\(invalid),\"hands\":[\(hands.joined(separator: ","))]}"
    }
}
