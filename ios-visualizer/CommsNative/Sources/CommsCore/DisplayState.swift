import Foundation

public struct DisplaySnapshot: Sendable {
    public let generation: UInt64
    public let revision: UInt64
    public let status: String
    public let result: GestureResult?
    public let receivedCount: UInt64
    /// Results waiting for CommsNextResult, and the waiting results lost because
    /// the queue was full (oldest dropped) or because they waited too long.
    public let pendingCount: Int
    public let pendingDropped: UInt64
    public let pendingExpired: UInt64
}

/// What CommsNextResult finds: nothing waiting, a result whose compact JSON and
/// terminating 0 need more room (it stays queued), or the bytes of the result taken.
public enum NextResult: Equatable, Sendable {
    case empty
    case needs(Int)
    case taken([UInt8])
}

/// All mutable state is protected by one lock. Networking callbacks may write
/// here while the Unity main thread polls; no timers or callbacks run under it.
public final class DisplayState: @unchecked Sendable {
    /// Provisional CommsNextResult limits: results waiting, and seconds from arrival.
    public static let pendingCapacity = 32
    public static let pendingLifetime: Double = 2
    private let lock = NSLock()
    private var generation: UInt64 = 0
    private var revision: UInt64 = 0
    private var status = "Disconnected"
    private var result: GestureResult?
    private var receivedAt: Double = 0
    private var observedTime: Double = 0
    private var receivedCount: UInt64 = 0
    private var active = false
    private var seen: Set<String> = []
    private var orderedIDs: [String] = []
    private var oldestID = 0
    // Accepted results in arrival order, each with its arrival time.
    private var pending: [(result: GestureResult, arrivedAt: Double)] = []
    private var pendingDropped: UInt64 = 0
    private var pendingExpired: UInt64 = 0

    public init() {}

    /// Begins an explicit user session. Reconnects should retain this generation
    /// and use clear instead, preserving deduplication across connection attempts.
    public func begin(status: String = "Connecting") -> UInt64 {
        lock.lock()
        defer { lock.unlock() }
        generation += 1
        revision += 1
        active = true
        self.status = status
        result = nil
        receivedAt = 0
        observedTime = 0
        receivedCount = 0
        seen.removeAll(keepingCapacity: true)
        orderedIDs.removeAll(keepingCapacity: true)
        oldestID = 0
        pending.removeAll(keepingCapacity: true)
        pendingDropped = 0
        pendingExpired = 0
        return generation
    }

    /// Ends the session: waiting results are discarded without being counted as lost.
    public func stop(status: String = "Disconnected") -> UInt64 {
        lock.lock()
        defer { lock.unlock() }
        generation += 1
        revision += 1
        active = false
        self.status = status
        result = nil
        pending.removeAll(keepingCapacity: true)
        return generation
    }

    @discardableResult
    public func accept(_ result: GestureResult, generation: UInt64, now: Double = ProcessInfo.processInfo.systemUptime) -> Bool {
        lock.lock()
        defer { lock.unlock() }
        // A poll can acquire the lock after a callback captured its timestamp.
        // Compare ordering with accepted arrivals, not the slightly later poll.
        guard active, generation == self.generation, now.isFinite, now >= receivedAt,
              max(observedTime, now) - now < 2,
              !seen.contains(result.resultID) else { return false }
        observedTime = max(observedTime, now)
        if orderedIDs.count == 4096 {
            seen.remove(orderedIDs[oldestID])
            orderedIDs[oldestID] = result.resultID
            oldestID = (oldestID + 1) % 4096
        } else {
            orderedIDs.append(result.resultID)
        }
        seen.insert(result.resultID)
        receivedCount += 1
        self.result = result
        receivedAt = now
        expirePending()
        if pending.count == DisplayState.pendingCapacity {
            pending.removeFirst()
            pendingDropped += 1
        }
        pending.append((result: result, arrivedAt: now))
        revision += 1
        return true
    }

    /// Automatic reconnect statuses keep the waiting results; they still expire.
    @discardableResult
    public func clear(generation: UInt64, status: String) -> Bool {
        lock.lock()
        defer { lock.unlock() }
        guard active, generation == self.generation else { return false }
        if result != nil || self.status != status { revision += 1 }
        result = nil
        self.status = status
        return true
    }

    @discardableResult
    public func setStatus(_ status: String, generation: UInt64) -> Bool {
        lock.lock()
        defer { lock.unlock() }
        guard active, generation == self.generation else { return false }
        if self.status != status { revision += 1 }
        self.status = status
        return true
    }

    public func snapshot(now: Double = ProcessInfo.processInfo.systemUptime) -> DisplaySnapshot {
        lock.lock()
        defer { lock.unlock() }
        if now.isFinite { observedTime = max(observedTime, now) }
        if result != nil, observedTime - receivedAt >= 2 {
            result = nil
            revision += 1
        }
        expirePending()
        return DisplaySnapshot(generation: generation, revision: revision, status: status, result: result, receivedCount: receivedCount,
                               pendingCount: pending.count, pendingDropped: pendingDropped, pendingExpired: pendingExpired)
    }

    /// Drops expired results, then takes the oldest waiting result if its compact
    /// JSON and a terminating 0 fit in `capacity` bytes; otherwise it stays queued.
    /// The JSON is built under the lock (pure computation, no callbacks), so the
    /// size check and the removal see the same oldest result.
    public func takeNext(capacity: Int, now: Double = ProcessInfo.processInfo.systemUptime) -> NextResult {
        lock.lock()
        defer { lock.unlock() }
        if now.isFinite { observedTime = max(observedTime, now) }
        expirePending()
        guard let oldest = pending.first else { return .empty }
        let bytes = Array(oldest.result.nextResultJSON.utf8)
        guard capacity > bytes.count else { return .needs(bytes.count + 1) }
        pending.removeFirst()
        return .taken(bytes)
    }

    // Call with the lock held. Arrivals are in time order (accept refuses an
    // earlier timestamp), so expired results are always at the front.
    private func expirePending() {
        while let oldest = pending.first, observedTime - oldest.arrivedAt >= DisplayState.pendingLifetime {
            pending.removeFirst()
            pendingExpired += 1
        }
    }
}
