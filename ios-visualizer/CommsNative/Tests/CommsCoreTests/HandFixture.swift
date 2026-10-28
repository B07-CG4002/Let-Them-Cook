import Foundation
@testable import CommsCore

/// HAND_EVENT bodies as the service sends them: compact JSON in the key order of
/// Wen Ree's EventEncoder (src/hand_events.py), with the defaults of a valid event.
enum HandFixture {
    static let epoch = "0123456789abcdef0123456789abcdef"
    static let otherEpoch = "fedcba9876543210fedcba9876543210"
    static let source = #"{"boot_id":11,"seq":120,"uptime_ms":2400}"#

    /// `state` is the n in a refresh's or an end's state_id "<epoch>:<device>:<n>".
    /// gesture, confidence, lease and reason default to valid values for the kind.
    static func body(_ kind: String, device: Int = 1, seq: UInt64 = 1, gesture: String? = nil, confidence: String? = nil,
                     source: String? = HandFixture.source, state: UInt64 = 1, lease: Int? = nil, reason: String? = nil,
                     epoch: String = HandFixture.epoch, session: String = "ltc-comms") -> String {
        let id = "\(epoch):\(device):\(seq)"
        let prediction = ["pulse", "state_start", "state_refresh", "idle"].contains(kind)
        let ongoing = kind == "state_start" || kind == "state_refresh"
        let defaultGesture: String? = kind == "pulse" ? "PICKUP" : kind == "idle" ? "IDLE" : kind == "reset" ? nil : "CHOP"
        let gestureText = (gesture ?? defaultGesture).map { "\"\($0)\"" } ?? "null"
        let confidenceText = confidence ?? (prediction ? "0.93" : "null")
        let stateText = kind == "state_start" ? "\"\(id)\"" : kind == "state_refresh" || kind == "state_end" ? "\"\(epoch):\(device):\(state)\"" : "null"
        let leaseValue = lease ?? (ongoing ? 1000 : 0)
        let reasonText = reason ?? (prediction ? "confirmed" : "confirmed_transition")
        return "{\"v\":4,\"type\":\"HAND_EVENT\",\"event_format\":1,\"session_id\":\"\(session)\",\"worker_epoch\":\"\(epoch)\","
            + "\"device_id\":\(device),\"event_seq\":\(seq),\"event_id\":\"\(id)\",\"kind\":\"\(kind)\",\"gesture\":\(gestureText),"
            + "\"confidence\":\(confidenceText),\"source\":\(source ?? "null"),\"state_id\":\(stateText),\"lease_ms\":\(leaseValue),"
            + "\"reason\":\"\(reasonText)\"}"
    }

    static func event(_ body: String, session: String = "ltc-comms") throws -> HandEvent {
        try CommsProtocol.handEvent(Array(body.utf8), session: session)
    }
}
