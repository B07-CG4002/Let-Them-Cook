import Foundation
import CommsCore

/// CommsHandState and CommsNextHandEvent, with the return convention of
/// CommsNextResult: the byte count written (a terminating 0 follows it), 0 when
/// nothing waits, or -(bytes + 1) when the buffer is null or smaller than bytes + 1;
/// nothing is then written and a waiting event stays queued. Takes only the hand
/// state's lock. The state JSON and one event's JSON are each under 1,024 bytes
/// (the longest cases are tested), so a 1,024-byte buffer always suffices.
final class HandMailbox {
    let state: HandEventState
    init(state: HandEventState) { self.state = state }

    /// Both hands' state as compact JSON (HandStateSnapshot.compactJSON); never 0.
    func handState(into buffer: UnsafeMutablePointer<CChar>?, capacity: Int32, now: Double = ProcessInfo.processInfo.systemUptime) -> Int32 {
        let bytes = Array(state.snapshot(now: now).compactJSON.utf8)
        guard let buffer = buffer, Int(capacity) > bytes.count else { return -Int32(bytes.count + 1) }
        write(bytes, into: buffer)
        return Int32(bytes.count)
    }

    /// The oldest accepted HAND_EVENT waiting, as compact JSON (HandEvent.compactJSON).
    func nextHandEvent(into buffer: UnsafeMutablePointer<CChar>?, capacity: Int32, now: Double = ProcessInfo.processInfo.systemUptime) -> Int32 {
        guard let buffer = buffer else {
            // No room at all: a waiting event is reported, never taken.
            if case .needs(let needed) = state.takeNext(capacity: 0, now: now) { return -Int32(needed) }
            return 0
        }
        switch state.takeNext(capacity: Int(capacity), now: now) {
        case .empty:
            return 0
        case .needs(let needed):
            return -Int32(needed)
        case .taken(let bytes):
            write(bytes, into: buffer)
            return Int32(bytes.count)
        }
    }

    private func write(_ bytes: [UInt8], into buffer: UnsafeMutablePointer<CChar>) {
        for (index, byte) in bytes.enumerated() { buffer[index] = CChar(bitPattern: byte) }
        buffer[bytes.count] = 0
    }
}
