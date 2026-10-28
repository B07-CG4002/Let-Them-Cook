import Foundation
import XCTest
@testable import CommsCore

final class HandEventTests: XCTestCase {
    private let epoch = HandFixture.epoch
    private let start = HandFixture.body("state_start", seq: 7)
    private let refresh = HandFixture.body("state_refresh", seq: 5, state: 3)
    private let end = HandFixture.body("state_end", seq: 5, state: 3)
    private let pulse = HandFixture.body("pulse", seq: 4)
    private let reset = HandFixture.body("reset", seq: 9, source: nil, reason: "manual_reset")
    private let result = #"{"v":3,"type":"GESTURE_RESULT","session_id":"ltc-comms","device_id":1,"boot_id":7,"seq":42,"request_id":null,"result_id":"1:7:42","gesture":"CHOP","confidence":0.87}"#
    // The backslash and "u" that begin a JSON unicode escape, built at run time.
    private let u = String(UnicodeScalar(92)) + "u"

    /// Every case edits a substring that must exist, so a fixture change cannot
    /// silently turn a rejection test into a test of the unchanged valid body.
    private func edit(_ body: String, _ old: String, _ new: String, file: StaticString = #filePath, line: UInt = #line) -> String {
        XCTAssertTrue(body.contains(old), "Fixture does not contain \(old)", file: file, line: line)
        return body.replacingOccurrences(of: old, with: new)
    }

    private func frame(_ body: String) throws -> SubscribedFrame {
        try CommsProtocol.subscribedFrame(Array(body.utf8), session: "ltc-comms")
    }

    func testParsesEveryKindWithItsFields() throws {
        let started = try HandFixture.event(start)
        XCTAssertEqual(started.json, start)
        XCTAssertEqual(started.session, "ltc-comms")
        XCTAssertEqual(started.workerEpoch, epoch)
        XCTAssertEqual(started.deviceID, 1)
        XCTAssertEqual(started.eventSeq, 7)
        XCTAssertEqual(started.eventID, "\(epoch):1:7")
        XCTAssertEqual(started.kind, .stateStart)
        XCTAssertEqual(started.gesture, "CHOP")
        XCTAssertEqual(started.confidence, 0.93)
        XCTAssertEqual(started.source, HandEvent.Source(bootID: 11, seq: 120, uptimeMS: 2400))
        XCTAssertEqual(started.stateID, started.eventID, "A state starts with its own event_id")
        XCTAssertEqual(started.leaseMS, 1000)
        XCTAssertEqual(started.reason, "confirmed")

        for gesture in ["PICKUP", "DROP", "THROW"] {
            let event = try HandFixture.event(HandFixture.body("pulse", device: 2, seq: 4, gesture: gesture))
            XCTAssertEqual(event.kind, .pulse)
            XCTAssertEqual(event.deviceID, 2)
            XCTAssertEqual(event.gesture, gesture)
            XCTAssertNil(event.stateID)
            XCTAssertEqual(event.leaseMS, 0)
        }
        for gesture in ["CHOP", "WASH", "POURING"] {
            for kind in ["state_start", "state_refresh", "state_end"] {
                let event = try HandFixture.event(HandFixture.body(kind, seq: 5, gesture: gesture, state: kind == "state_start" ? 5 : 2))
                XCTAssertEqual(event.gesture, gesture)
                XCTAssertEqual(event.kind.rawValue, kind)
            }
        }
        let refreshed = try HandFixture.event(refresh)
        XCTAssertEqual(refreshed.stateID, "\(epoch):1:3")
        XCTAssertEqual(refreshed.confidence, 0.93)
        XCTAssertEqual(refreshed.leaseMS, 1000)
        for lease in [100, 2000] {
            XCTAssertEqual(try HandFixture.event(HandFixture.body("state_refresh", seq: 5, state: 5, lease: lease)).leaseMS, UInt32(lease))
        }
        for reason in ["confirmed_transition", "prediction_timeout", "source_reset", "connection_reset", "worker_disconnect", "manual_reset"] {
            let ended = try HandFixture.event(HandFixture.body("state_end", seq: 5, state: 3, reason: reason))
            XCTAssertEqual(ended.kind, .stateEnd)
            XCTAssertNil(ended.confidence)
            XCTAssertEqual(ended.stateID, "\(epoch):1:3")
            XCTAssertEqual(ended.leaseMS, 0)
            XCTAssertEqual(ended.reason, reason)
            XCTAssertEqual(try HandFixture.event(HandFixture.body("reset", seq: 9, source: nil, reason: reason)).reason, reason)
        }
        let idle = try HandFixture.event(HandFixture.body("idle", seq: 2))
        XCTAssertEqual(idle.kind, .idle)
        XCTAssertEqual(idle.gesture, "IDLE")
        XCTAssertEqual(idle.reason, "confirmed")
        let cleared = try HandFixture.event(reset)
        XCTAssertEqual(cleared.kind, .reset)
        XCTAssertNil(cleared.gesture)
        XCTAssertNil(cleared.confidence)
        XCTAssertNil(cleared.source)
        XCTAssertNil(cleared.stateID)
        // Wen Ree's EventEncoder.reset copies the hand's last source before clearing it,
        // and his validate_event accepts a reset with or without one.
        XCTAssertNotNil(try HandFixture.event(HandFixture.body("reset", seq: 9, reason: "connection_reset")).source)
        // Endpoints: event_seq 1 and 2^53 - 1, uint32 source values 0 and 4294967295.
        let largest = try HandFixture.event(HandFixture.body("state_refresh", seq: 9_007_199_254_740_991,
                                                             source: #"{"boot_id":4294967295,"seq":0,"uptime_ms":4294967295}"#,
                                                             state: 9_007_199_254_740_991))
        XCTAssertEqual(largest.eventSeq, CommsProtocol.maximumEventSeq)
        XCTAssertEqual(largest.stateID, "\(epoch):1:9007199254740991")
        XCTAssertEqual(largest.source, HandEvent.Source(bootID: UInt32.max, seq: 0, uptimeMS: UInt32.max))
        for confidence in ["0", "1", "1e0", "0.5", "100e-2"] {
            XCTAssertNotNil(try HandFixture.event(HandFixture.body("pulse", confidence: confidence)).confidence)
        }
    }

    func testTransitionPairOnOneSourceSampleIsTwoEvents() throws {
        // CHOP ends and POURING starts on the same glove sample.
        let ended = try HandFixture.event(HandFixture.body("state_end", seq: 5, gesture: "CHOP", state: 3, reason: "confirmed_transition"))
        let started = try HandFixture.event(HandFixture.body("state_start", seq: 6, gesture: "POURING"))
        XCTAssertEqual(ended.source, started.source)
        XCTAssertNotEqual(ended.eventID, started.eventID)
        XCTAssertEqual(ended.stateID, "\(epoch):1:3")
        XCTAssertEqual(started.stateID, "\(epoch):1:6")
    }

    func testRejectsEachMalformedField() {
        let id = "\(epoch):1:7"
        let cases: [(String, String, String)] = [
            (start, #""v":4"#, #""v":3"#), (start, #""v":4"#, #""v":"4""#), (start, #""v":4"#, #""v":4.0"#), (start, #""v":4"#, #""v":null"#),
            (start, #""type":"HAND_EVENT""#, #""type":"GESTURE_RESULT""#), (start, #""type":"HAND_EVENT""#, #""type":"hand_event""#),
            (start, #""event_format":1"#, #""event_format":2"#), (start, #""event_format":1"#, #""event_format":"1""#),
            (start, #""event_format":1"#, #""event_format":1.0"#), (start, #""event_format":1"#, #""event_format":null"#),
            (start, #""session_id":"ltc-comms""#, #""session_id":"other""#),
            (start, #""device_id":1"#, #""device_id":"1""#), (start, #""device_id":1"#, #""device_id":1.0"#),
            (start, #""device_id":1"#, #""device_id":-1"#),
            (start, #""event_seq":7"#, #""event_seq":"7""#), (start, #""event_seq":7"#, #""event_seq":7.0"#),
            (start, #""event_seq":7"#, #""event_seq":7e0"#), (start, #""event_seq":7"#, #""event_seq":-7"#),
            (start, #""event_seq":7"#, #""event_seq":07"#), (start, #""event_seq":7"#, #""event_seq":null"#),
            (start, "\"event_id\":\"\(id)\"", "\"event_id\":\"\(epoch):1:8\""),
            (start, "\"event_id\":\"\(id)\"", "\"event_id\":\"\(epoch):2:7\""),
            (start, "\"event_id\":\"\(id)\"", "\"event_id\":\"\(id) \""),
            (start, "\"event_id\":\"\(id)\"", "\"event_id\":\"\(epoch.uppercased()):1:7\""),
            (start, "\"event_id\":\"\(id)\"", #""event_id":null"#),
            (start, #""kind":"state_start""#, #""kind":"start""#), (start, #""kind":"state_start""#, #""kind":"STATE_START""#),
            (start, #""kind":"state_start""#, #""kind":"state-start""#), (start, #""kind":"state_start""#, #""kind":null"#),
            (start, #""gesture":"CHOP""#, #""gesture":"PICKUP""#), (start, #""gesture":"CHOP""#, #""gesture":"IDLE""#),
            (start, #""gesture":"CHOP""#, #""gesture":"chop""#), (start, #""gesture":"CHOP""#, #""gesture":"CHOP ""#),
            (start, #""gesture":"CHOP""#, #""gesture":"""#), (start, #""gesture":"CHOP""#, #""gesture":null"#),
            (start, #""gesture":"CHOP""#, #""gesture":7"#), (start, #""gesture":"CHOP""#, #""gesture":"REST""#),
            (pulse, #""gesture":"PICKUP""#, #""gesture":"CHOP""#), (pulse, #""gesture":"PICKUP""#, #""gesture":"IDLE""#),
            (refresh, #""gesture":"CHOP""#, #""gesture":"THROW""#), (end, #""gesture":"CHOP""#, #""gesture":"IDLE""#),
            (end, #""gesture":"CHOP""#, #""gesture":null"#), (reset, #""gesture":null"#, #""gesture":"CHOP""#),
            (start, #""confidence":0.93"#, #""confidence":null"#), (start, #""confidence":0.93"#, #""confidence":"0.93""#),
            (start, #""confidence":0.93"#, #""confidence":-0.01"#), (start, #""confidence":0.93"#, #""confidence":1.01"#),
            (start, #""confidence":0.93"#, #""confidence":1e999"#), (start, #""confidence":0.93"#, #""confidence":true"#),
            (pulse, #""confidence":0.93"#, #""confidence":null"#), (end, #""confidence":null"#, #""confidence":0.5"#),
            (reset, #""confidence":null"#, #""confidence":0"#),
            (start, HandFixture.source, "null"), (pulse, HandFixture.source, "null"), (end, HandFixture.source, "null"),
            (start, HandFixture.source, "{}"), (start, HandFixture.source, #"{"boot_id":11,"seq":120}"#),
            (start, HandFixture.source, #"{"boot_id":11,"seq":120,"uptime_ms":2400,"extra":1}"#),
            (start, HandFixture.source, #"{"boot_id":11,"seq":120,"uptime":2400}"#),
            (start, #""boot_id":11"#, #""boot_id":-1"#), (start, #""boot_id":11"#, #""boot_id":4294967296"#),
            (start, #""seq":120"#, #""seq":1.5"#), (start, #""seq":120"#, #""seq":"120""#), (start, #""seq":120"#, #""seq":null"#),
            (start, #""uptime_ms":2400"#, #""uptime_ms":2400.0"#),
            (start, HandFixture.source, #""source""#), (start, HandFixture.source, "11"),
            (start, "\"state_id\":\"\(id)\"", "\"state_id\":\"\(epoch):1:6\""), (start, "\"state_id\":\"\(id)\"", #""state_id":null"#),
            (refresh, "\(epoch):1:3\"", "\(epoch):1:6\""), (refresh, "\(epoch):1:3\"", "\(epoch):1:0\""),
            (refresh, "\(epoch):1:3\"", "\(epoch):1:03\""), (refresh, "\(epoch):1:3\"", "\(epoch):1:\""),
            (refresh, "\(epoch):1:3\"", "\(epoch):1:3 \""), (refresh, "\(epoch):1:3\"", "\(epoch):1:+3\""),
            (refresh, "\(epoch):1:3\"", "\(epoch):1:12345678901234567\""), (refresh, "\(epoch):1:3\"", "\(epoch):2:3\""),
            (refresh, "\"\(epoch):1:3\"", "\"\(HandFixture.otherEpoch):1:3\""), (refresh, "\"\(epoch):1:3\"", "null"),
            (end, "\"\(epoch):1:3\"", "null"), (end, "\(epoch):1:3\"", "\(epoch):1:3:3\""),
            (pulse, #""state_id":null"#, "\"state_id\":\"\(epoch):1:4\""), (reset, #""state_id":null"#, "\"state_id\":\"\(epoch):1:9\""),
            (start, #""lease_ms":1000"#, #""lease_ms":99"#), (start, #""lease_ms":1000"#, #""lease_ms":2001"#),
            (start, #""lease_ms":1000"#, #""lease_ms":0"#), (start, #""lease_ms":1000"#, #""lease_ms":1000.0"#),
            (start, #""lease_ms":1000"#, #""lease_ms":null"#), (refresh, #""lease_ms":1000"#, #""lease_ms":0"#),
            (pulse, #""lease_ms":0"#, #""lease_ms":1000"#), (end, #""lease_ms":0"#, #""lease_ms":1000"#),
            (reset, #""lease_ms":0"#, #""lease_ms":100"#), (pulse, #""lease_ms":0"#, #""lease_ms":-0"#),
            (start, #""reason":"confirmed""#, #""reason":"confirmed_transition""#), (pulse, #""reason":"confirmed""#, #""reason":"manual_reset""#),
            (end, #""reason":"confirmed_transition""#, #""reason":"confirmed""#), (end, #""reason":"confirmed_transition""#, #""reason":"lease_expired""#),
            (reset, #""reason":"manual_reset""#, #""reason":"confirmed""#), (start, #""reason":"confirmed""#, #""reason":null"#),
            (start, #""reason":"confirmed""#, #""reason":"Confirmed""#),
            // Missing, extra and repeated keys, including a repeat spelled with an escape.
            (start, #","reason":"confirmed""#, ""), (start, #""reason":"confirmed""#, #""reason":"confirmed","extra":1"#),
            (start, #""kind":"state_start""#, #""kind":"state_start","kind":"state_start""#),
            (start, #""lease_ms":1000"#, #""lease_ms":1000,"lease_ms":1000"#),
            (start, #""lease_ms":1000"#, "\"lease_ms\":1000,\"\(u)006cease_ms\":1000"),
        ]
        for (body, old, new) in cases {
            XCTAssertThrowsError(try HandFixture.event(edit(body, old, new)), "Accepted \(new)")
        }
        for body in [HandFixture.body("state_start", epoch: epoch.uppercased()), HandFixture.body("state_start", epoch: String(epoch.dropLast())),
                     HandFixture.body("state_start", epoch: epoch + "0"), HandFixture.body("state_start", epoch: "g" + epoch.dropFirst()),
                     HandFixture.body("state_start", device: 0), HandFixture.body("state_start", device: 3),
                     HandFixture.body("state_start", seq: 0), HandFixture.body("pulse", seq: 9_007_199_254_740_992),
                     HandFixture.body("pulse", seq: 10_000_000_000_000_000), HandFixture.body("state_refresh", seq: 3, state: 4)] {
            XCTAssertThrowsError(try HandFixture.event(body), "Accepted \(body)")
        }
        XCTAssertThrowsError(try HandFixture.event(start, session: "other"))
        // A key spelled with an escape is the same key: this is still the source object.
        XCTAssertEqual(try HandFixture.event(edit(start, #""source":{"#, "\"\(u)0073ource\":{")).source?.seq, 120)
    }

    func testOnlySourceMayHoldAnObject() {
        let nested = [
            (start, #""gesture":"CHOP""#, #""gesture":{}"#), (start, #""confidence":0.93"#, #""confidence":{"v":1}"#),
            (start, "\"state_id\":\"\(epoch):1:7\"", #""state_id":{}"#), (start, #""reason":"confirmed""#, #""reason":{}"#),
            (start, #""session_id":"ltc-comms""#, #""session_id":{}"#), (start, #""event_format":1"#, #""event_format":{}"#),
            (start, #""boot_id":11"#, #""boot_id":{"seq":1}"#), (start, #""boot_id":11"#, #""boot_id":{}"#),
            (start, #""gesture":"CHOP""#, #""gesture":["CHOP"]"#), (start, HandFixture.source, "[11,120,2400]"),
            (start, #""reason":"confirmed""#, #""reason":"confirmed","extra":{}"#),
        ]
        for (body, old, new) in nested {
            XCTAssertThrowsError(try HandFixture.event(edit(body, old, new)), "Accepted \(new)")
        }
        // GESTURE_RESULT parsing is unchanged: no nested object and no eleventh key.
        for body in [edit(result, #""request_id":null"#, #""request_id":{}"#),
                     edit(result, #""request_id":null"#, #""request_id":null,"source":{"boot_id":7,"seq":42,"uptime_ms":1}"#),
                     edit(result, #""request_id":null"#, #""request_id":null,"source":null"#),
                     start, reset] {
            XCTAssertThrowsError(try CommsProtocol.result(Array(body.utf8), session: "ltc-comms"), "Accepted \(body)")
        }
        XCTAssertEqual(try CommsProtocol.result(Array(result.utf8), session: "ltc-comms").resultID, "1:7:42")
    }

    func testSubscribedFrameDispatchesByType() throws {
        XCTAssertEqual(try frame(result), .result(try CommsProtocol.result(Array(result.utf8), session: "ltc-comms")))
        XCTAssertEqual(try frame(start), .handEvent(try HandFixture.event(start)))
        XCTAssertEqual(try frame(reset), .handEvent(try HandFixture.event(reset)))
        // An invalid HAND_EVENT never throws, however it fails, once its top-level type was read...
        let invalid = [edit(pulse, #""gesture":"PICKUP""#, #""gesture":"CHOP""#),
                       edit(start, #""session_id":"ltc-comms""#, #""session_id":"other""#),
                       edit(start, #""v":4"#, #""v":3"#),
                       edit(start, #""gesture":"CHOP""#, #""gesture":{}"#),
                       edit(start, #""reason":"confirmed""#, #""reason":"confirmed","extra":1"#),
                       edit(start, #""kind":"state_start""#, #""kind":"state_start","kind":"pulse""#),
                       edit(start, #""reason":"confirmed"}"#, #""reason":"confirmed""#),
                       edit(start, #""reason":"confirmed"}"#, #""reason":"confirmed"}{}"#),
                       #"{"type":"HAND_EVENT"}"#, #"{"v":4,"type":"HAND_EVENT","#]
        // Nor does a well-formed JSON object whose top-level type is HAND_EVENT when the strict
        // parse stops before that type: a non-flat value, too many keys, or another key order.
        let typeLast = edit(edit(start, #""type":"HAND_EVENT","#, ""), #""reason":"confirmed"}"#, #""reason":"confirmed","type":"HAND_EVENT"}"#)
        let sixteenKeys = "{" + (0..<16).map { "\"k\($0)\":1" }.joined(separator: ",") + #","type":"HAND_EVENT"}"#
        let beforeType = [edit(start, #""v":4"#, #""v":[4]"#), edit(start, #""v":4"#, #""v":true"#), edit(start, #""v":4"#, #""v":1e999"#),
                          edit(start, #""v":4,"#, #""v":4,"extra":{"a":[1,{"b":null}],"c":false},"#),
                          edit(typeLast, #""confidence":0.93"#, #""confidence":false"#), edit(typeLast, HandFixture.source, "[11,120,2400]"),
                          sixteenKeys, #"{"v":[4],"type":"HAND_EVENT"}"#, #" { "v" : [ ] , "type" : "HAND_EVENT" } "#]
        for body in invalid + beforeType {
            guard case .invalidHandEvent = try frame(body) else { XCTFail("Not an invalid hand event: \(body)"); continue }
        }
        // A valid event is valid in any key order.
        XCTAssertEqual(try frame(typeLast), .handEvent(try HandFixture.event(typeLast)))
        // Everything that is not a HAND_EVENT fails exactly as on a version 1 subscription.
        let deep = #"{"v":"# + String(repeating: "[", count: 65) + String(repeating: "]", count: 65) + #","type":"HAND_EVENT"}"#
        let failing = [edit(result, #""seq":42"#, #""seq":"42""#),
                       edit(result, #""type":"GESTURE_RESULT""#, #""type":"MONITOR_UPDATE""#),
                       #"{"v":1,"type":"SUBSCRIBED","session_id":"ltc-comms"}"#,
                       edit(start, #""type":"HAND_EVENT","#, ""),
                       edit(start, #""type":"HAND_EVENT""#, #""type":"HAND_EVENT ""#),
                       // A type inside a nested object is not the frame's type.
                       #"{"source":{"type":"HAND_EVENT"}}"#, #"{"v":{"type":"HAND_EVENT"}}"#, #"{"v":[{"type":"HAND_EVENT"}]}"#,
                       #"{"type":"HAND_EVENT","#.replacingOccurrences(of: "{", with: "["),
                       // Before the type is read, only well-formed JSON with unique keys and at most
                       // 64 levels of nesting counts as a HAND_EVENT; anything else fails as before.
                       #"{"v":4,"x":,"type":"HAND_EVENT"}"#, #"{"v":[4,],"type":"HAND_EVENT"}"#, #"{"v":tru,"type":"HAND_EVENT"}"#,
                       #"{"v":[4],"v":[4],"type":"HAND_EVENT"}"#, #"{"v":{"a":1,"a":2},"type":"HAND_EVENT"}"#,
                       #"{"v":[4],"type":"HAND_EVENT"}x"#, #"{"v":[4],"type":"HAND_EVENT"}{}"#, #"{"v":[4],"type":["HAND_EVENT"]}"#,
                       #"{"v":[4],"type":"HAND_EVENT","type":"HAND_EVENT"}"#, #"{"v":[4],"type":"GESTURE_RESULT"}"#, deep]
        for body in failing {
            XCTAssertThrowsError(try frame(body), "Did not fail \(body)")
        }
        for bytes: [UInt8] in [[], [0xFF], Array(#"{"type":"HAND_EVENT","x":""#.utf8) + [0xC0, 0xAF, 0x22, 0x7D]] {
            XCTAssertThrowsError(try CommsProtocol.subscribedFrame(bytes, session: "ltc-comms"))
        }
        XCTAssertThrowsError(try CommsProtocol.subscribedFrame([UInt8](repeating: 0x20, count: 16_385), session: "ltc-comms"))
    }

    func testCompactJSONIsTheWholeEnvelopeInOrder() throws {
        for body in [start, refresh, end, pulse, reset, HandFixture.body("idle", device: 2, seq: 3),
                     HandFixture.body("reset", seq: 2, reason: "worker_disconnect")] {
            XCTAssertEqual(try HandFixture.event(body).compactJSON, body)
        }
        // Whitespace and escapes are normalized; the fields read back unchanged.
        let spaced = edit(edit(start, #""kind":"state_start""#, " \"kind\" : \"st\(u)0061te_start\" "), #""confidence":0.93"#, #""confidence":93e-2"#)
        let event = try HandFixture.event(spaced)
        XCTAssertEqual(event.compactJSON, start)
        XCTAssertEqual(try HandFixture.event(event.compactJSON), try HandFixture.event(start))
        let one = try HandFixture.event(HandFixture.body("pulse", confidence: "1"))
        XCTAssertTrue(one.compactJSON.contains(#""confidence":1.0,"#))
        // A session with a quote and a backslash is escaped and reads back.
        let session = #"a"b\c"#
        let quoted = try HandFixture.event(HandFixture.body("pulse", session: #"a\"b\\c"#), session: session)
        let object = try XCTUnwrap(JSONSerialization.jsonObject(with: Data(quoted.compactJSON.utf8)) as? [String: Any])
        XCTAssertEqual(object["session_id"] as? String, session)
        XCTAssertEqual(object.count, 15)
        XCTAssertEqual(try HandFixture.event(quoted.compactJSON, session: session), quoted.withJSON(quoted.compactJSON))
    }
}

private extension HandEvent {
    func withJSON(_ json: String) -> HandEvent {
        HandEvent(json: json, session: session, workerEpoch: workerEpoch, deviceID: deviceID, eventSeq: eventSeq, eventID: eventID, kind: kind,
                  gesture: gesture, confidence: confidence, source: source, stateID: stateID, leaseMS: leaseMS, reason: reason)
    }
}
