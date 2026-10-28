import Foundation
import CommsCore

final class DisplayMailbox {
    let state: DisplayState
    private let lock = NSLock()
    private var delivered: String?
    init(state: DisplayState) { self.state = state }

    func copy(into buffer: UnsafeMutablePointer<CChar>?, capacity: Int32) -> Int32 {
        guard let buffer = buffer, capacity > 1 else { return 0 }
        lock.lock(); defer { lock.unlock() }
        let snapshot = state.snapshot()
        let live: String
        if let result = snapshot.result {
            live = "\(result.gesture)  |  \(result.resultID)  |  confidence \(result.confidence)"
        } else { live = "No live result" }
        let text = "Communications\n\(snapshot.status)\n\(live)\nReceived: \(snapshot.receivedCount)"
        let bytes = Array(text.utf8)
        guard text != delivered, bytes.count < Int(capacity) else { return 0 }
        for (index, byte) in bytes.enumerated() { buffer[index] = CChar(bitPattern: byte) }
        buffer[bytes.count] = 0
        delivered = text
        return Int32(bytes.count)
    }

    /// CommsNextResult: the oldest waiting result as compact JSON plus a terminating 0.
    /// Returns its byte count, 0 when nothing waits, or -(bytes + 1) when the buffer
    /// is null or smaller than bytes + 1; that result then stays queued and nothing
    /// is written. Takes only the state lock, never the display lock.
    func nextResult(into buffer: UnsafeMutablePointer<CChar>?, capacity: Int32, now: Double = ProcessInfo.processInfo.systemUptime) -> Int32 {
        guard let buffer = buffer else {
            // No room at all: a waiting result is reported, never taken.
            if case .needs(let needed) = state.takeNext(capacity: 0, now: now) { return -Int32(needed) }
            return 0
        }
        switch state.takeNext(capacity: Int(capacity), now: now) {
        case .empty:
            return 0
        case .needs(let needed):
            return -Int32(needed)
        case .taken(let bytes):
            for (index, byte) in bytes.enumerated() { buffer[index] = CChar(bitPattern: byte) }
            buffer[bytes.count] = 0
            return Int32(bytes.count)
        }
    }

    func resetDelivery() { lock.lock(); delivered = nil; lock.unlock() }
}

private let sharedState = DisplayState()
private let sharedMailbox = DisplayMailbox(state: sharedState)
private let sharedHands = HandEventState()
private let sharedHandMailbox = HandMailbox(state: sharedHands)

@_cdecl("CommsStart")
public func commsStart() {
    sharedMailbox.resetDelivery()
    #if os(iOS)
    DispatchQueue.main.async { IntegrationController.shared.install(state: sharedState, hands: sharedHands) }
    #else
    _ = sharedState.stop(status: "iPhone setup required")
    _ = sharedHands.stop()
    #endif
}

@_cdecl("CommsCopyDisplay")
public func commsCopyDisplay(_ buffer: UnsafeMutablePointer<CChar>?, _ capacity: Int32) -> Int32 {
    sharedMailbox.copy(into: buffer, capacity: capacity)
}

// int32_t CommsNextResult(char* buffer, int32_t capacity); never blocks, safe every frame.
@_cdecl("CommsNextResult")
public func commsNextResult(_ buffer: UnsafeMutablePointer<CChar>?, _ capacity: Int32) -> Int32 {
    sharedMailbox.nextResult(into: buffer, capacity: capacity)
}

// int32_t CommsHandState(char* buffer, int32_t capacity); both hands' state as compact
// JSON, same return convention as CommsNextResult (never 0). Never blocks, safe every frame.
@_cdecl("CommsHandState")
public func commsHandState(_ buffer: UnsafeMutablePointer<CChar>?, _ capacity: Int32) -> Int32 {
    sharedHandMailbox.handState(into: buffer, capacity: capacity)
}

// int32_t CommsNextHandEvent(char* buffer, int32_t capacity); the next accepted HAND_EVENT
// as compact JSON, same return convention as CommsNextResult. Never blocks, safe every frame.
@_cdecl("CommsNextHandEvent")
public func commsNextHandEvent(_ buffer: UnsafeMutablePointer<CChar>?, _ capacity: Int32) -> Int32 {
    sharedHandMailbox.nextHandEvent(into: buffer, capacity: capacity)
}

@_cdecl("CommsStop")
public func commsStop() {
    // Invalidate immediately, before a queued UIKit teardown can run.
    _ = sharedState.stop(status: "Stopped")
    _ = sharedHands.stop()
    #if os(iOS)
    DispatchQueue.main.async { IntegrationController.shared.uninstall() }
    #endif
}

// Existing Unity exports still call these names until the game is rebuilt.
@_cdecl("Week7Start")
public func week7Start() { commsStart() }

@_cdecl("Week7CopyDisplay")
public func week7CopyDisplay(_ buffer: UnsafeMutablePointer<CChar>?, _ capacity: Int32) -> Int32 {
    commsCopyDisplay(buffer, capacity)
}

@_cdecl("Week7Stop")
public func week7Stop() { commsStop() }
