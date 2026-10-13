import Foundation

public struct DisplaySnapshot: Sendable {
    public let generation: UInt64
    public let revision: UInt64
    public let status: String
    public let result: GestureResult?
    public let receivedCount: UInt64
}

/// All mutable state is protected by one lock. Networking callbacks may write
/// here while the Unity main thread polls; no timers or callbacks run under it.
public final class DisplayState: @unchecked Sendable {
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
        return generation
    }

    public func stop(status: String = "Disconnected") -> UInt64 {
        lock.lock()
        defer { lock.unlock() }
        generation += 1
        revision += 1
        active = false
        self.status = status
        result = nil
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
        revision += 1
        return true
    }

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
        return DisplaySnapshot(generation: generation, revision: revision, status: status, result: result, receivedCount: receivedCount)
    }
}
