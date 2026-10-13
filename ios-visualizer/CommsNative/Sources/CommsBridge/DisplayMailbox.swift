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
            live = "\(result.gesture)  |  \(result.resultID)  |  confidence 1.0"
        } else { live = "No live result" }
        let text = "Communications • DUMMY inference\n\(snapshot.status)\n\(live)\nReceived: \(snapshot.receivedCount)"
        let bytes = Array(text.utf8)
        guard text != delivered, bytes.count < Int(capacity) else { return 0 }
        for (index, byte) in bytes.enumerated() { buffer[index] = CChar(bitPattern: byte) }
        buffer[bytes.count] = 0
        delivered = text
        return Int32(bytes.count)
    }

    func resetDelivery() { lock.lock(); delivered = nil; lock.unlock() }
}

private let sharedState = DisplayState()
private let sharedMailbox = DisplayMailbox(state: sharedState)

@_cdecl("CommsStart")
public func commsStart() {
    sharedMailbox.resetDelivery()
    #if os(iOS)
    DispatchQueue.main.async { IntegrationController.shared.install(state: sharedState) }
    #else
    _ = sharedState.stop(status: "iPhone setup required")
    #endif
}

@_cdecl("CommsCopyDisplay")
public func commsCopyDisplay(_ buffer: UnsafeMutablePointer<CChar>?, _ capacity: Int32) -> Int32 {
    sharedMailbox.copy(into: buffer, capacity: capacity)
}

@_cdecl("CommsStop")
public func commsStop() {
    // Invalidate immediately, before a queued UIKit teardown can run.
    _ = sharedState.stop(status: "Stopped")
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
