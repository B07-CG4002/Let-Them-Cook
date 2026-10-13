import XCTest
@testable import CommsCore

final class ProtocolTests: XCTestCase {
    private let subscribed = #"{"v":1,"type":"SUBSCRIBED","session_id":"ltc-comms"}"#
    private let result = #"{"v":1,"type":"GESTURE_RESULT","session_id":"ltc-comms","device_id":1,"boot_id":7,"seq":42,"result_id":"1:7:42","gesture":"OPEN","confidence":1.0}"#

    func testSubscribeEmitsOneLengthPrefixedRequest() throws {
        let expected = Array(#"{"v":1,"type":"SUBSCRIBE","session_id":"ltc-comms"}"#.utf8)
        XCTAssertEqual(try CommsProtocol.subscribe(session: "ltc-comms"), [0, 0, 0, 51] + expected)
    }

    func testLiteralSubscribedAndDeterministicResult() throws {
        try CommsProtocol.subscribed(Array(subscribed.utf8), session: "ltc-comms")
        let message = try CommsProtocol.result(Array(result.utf8), session: "ltc-comms")
        XCTAssertEqual(message.resultID, "1:7:42")
        XCTAssertEqual(message.gesture, "OPEN")
        XCTAssertEqual(message.seq, 42)
        XCTAssertEqual(message.deviceID, 1)
        XCTAssertEqual(message.bootID, 7)
        XCTAssertEqual(message.json, result)
    }

    func testV2RandomLabelsAndCommandIdentityPreserveCorrelation() throws {
        let stream = result.replacingOccurrences(of: #""v":1"#, with: #""v":2,"request_id":null"#)
        for label in ["REST", "FIST", "OPEN", "POINT"] {
            let message = try CommsProtocol.result(Array(stream.replacingOccurrences(of: "OPEN", with: label).utf8), session: "ltc-comms")
            XCTAssertEqual(message.gesture, label)
            XCTAssertNil(message.requestID)
        }
        let command = stream.replacingOccurrences(of: #""request_id":null"#, with: #""request_id":42"#)
            .replacingOccurrences(of: #""1:7:42""#, with: #""cmd:1:7:42""#)
        let parsed = try CommsProtocol.result(Array(command.utf8), session: "ltc-comms")
        XCTAssertEqual(parsed.requestID, 42)
        XCTAssertEqual(parsed.resultID, "cmd:1:7:42")
        for request in ["true", "-1", "42.0", "4294967296", "41", #""42""#] {
            XCTAssertThrowsError(try CommsProtocol.result(Array(command.replacingOccurrences(of: #""request_id":42"#, with: "\"request_id\":\(request)").utf8), session: "ltc-comms"))
        }
        for invalid in [stream.replacingOccurrences(of: #","request_id":null"#, with: ""),
                        stream.replacingOccurrences(of: "OPEN", with: "OTHER"),
                        stream.replacingOccurrences(of: #""v":2"#, with: #""v":1"#),
                        stream.replacingOccurrences(of: #""request_id":null"#, with: #""request_id":null,"request_id":null"#)] {
            XCTAssertThrowsError(try CommsProtocol.result(Array(invalid.utf8), session: "ltc-comms"))
        }
    }

    func testAcceptsExactUInt32EndpointsAndAllDeterministicGestures() throws {
        let literals = [
            #"{"v":1,"type":"GESTURE_RESULT","session_id":"ltc-comms","device_id":2,"boot_id":0,"seq":0,"result_id":"2:0:0","gesture":"REST","confidence":1}"#,
            #"{"v":1,"type":"GESTURE_RESULT","session_id":"ltc-comms","device_id":1,"boot_id":4294967295,"seq":1,"result_id":"1:4294967295:1","gesture":"FIST","confidence":1e0}"#,
            #"{"v":1,"type":"GESTURE_RESULT","session_id":"ltc-comms","device_id":2,"boot_id":4294967295,"seq":4294967295,"result_id":"2:4294967295:4294967295","gesture":"POINT","confidence":1.00}"#,
        ]
        for (body, expected) in zip(literals, [UInt32(0), 1, UInt32.max]) {
            XCTAssertEqual(try CommsProtocol.result(Array(body.utf8), session: "ltc-comms").seq, expected)
        }
    }

    func testRejectsWrongTypesSchemaSessionAndDummyContract() {
        let replacements = [
            (#""v":1"#, #""v":true"#),
            (#""v":1"#, #""v":1.0"#),
            (#""v":1"#, #""v":2"#),
            (#""seq":42"#, #""seq":"42""#),
            (#""seq":42"#, #""seq":42.0"#),
            (#""seq":42"#, #""seq":42e0"#),
            (#""seq":42"#, #""seq":-0"#),
            (#""seq":42"#, #""seq":4294967296"#),
            (#""seq":42"#, #""seq":999999999999999999999999999"#),
            (#""boot_id":7"#, #""boot_id":-1"#),
            (#""boot_id":7"#, #""boot_id":4294967296"#),
            (#""device_id":1"#, #""device_id":0"#),
            (#""device_id":1"#, #""device_id":3"#),
            (#""confidence":1.0"#, #""confidence":true"#),
            (#""confidence":1.0"#, #""confidence":"1.0""#),
            (#""confidence":1.0"#, #""confidence":null"#),
            (#""confidence":1.0"#, #""confidence":{}"#),
            (#""confidence":1.0"#, #""confidence":[]"#),
            (#""confidence":1.0"#, #""confidence":0.5"#),
            (#""confidence":1.0"#, #""confidence":1e999"#),
            (#""confidence":1.0"#, #""confidence":NaN"#),
            (#""confidence":1.0"#, #""confidence":Infinity"#),
            (#""gesture":"OPEN""#, #""gesture":"FIST""#),
            (#""result_id":"1:7:42""#, #""result_id":"01:7:42""#),
            (#""type":"GESTURE_RESULT""#, #""type":"SUBSCRIBED""#),
            (#""session_id":"ltc-comms""#, #""session_id":"other""#),
            (#""confidence":1.0"#, #""confidence":1.0,"extra":1"#),
            (#""confidence":1.0"#, #""score":1.0"#),
        ]
        for (old, new) in replacements {
            XCTAssertThrowsError(try CommsProtocol.result(Array(result.replacingOccurrences(of: old, with: new).utf8), session: "ltc-comms"), "Accepted \(new)")
        }
    }

    func testRejectsDuplicateDecodedKeysAndMalformedJSON() {
        let bodies = [
            result.replacingOccurrences(of: #""seq":42"#, with: #""seq":42,"seq":42"#),
            result.replacingOccurrences(of: #""seq":42"#, with: #""seq":42,"\u0073eq":42"#),
            result.replacingOccurrences(of: #""seq":42"#, with: #""seq":042"#),
            result.replacingOccurrences(of: #""seq":42"#, with: #""seq":+42"#),
            result.replacingOccurrences(of: #""confidence":1.0"#, with: #""confidence":1."#),
            result.replacingOccurrences(of: #""confidence":1.0"#, with: #""confidence":1e"#),
            result.replacingOccurrences(of: #""confidence":1.0"#, with: #""confidence":.1"#),
            result + "{}", "[" + result + "]", String(result.dropLast()) + ",}",
            result.replacingOccurrences(of: "OPEN", with: #"\q"#),
            result.replacingOccurrences(of: "OPEN", with: #"\uD800"#),
            result.replacingOccurrences(of: "OPEN", with: #"\uDC00"#),
            result.replacingOccurrences(of: "OPEN", with: #"\uD800\u0041"#),
            result.replacingOccurrences(of: "OPEN", with: "OP\nEN"),
            "\u{FEFF}" + result,
            String(repeating: "[", count: 16000),
        ]
        for (index, body) in bodies.enumerated() {
            XCTAssertThrowsError(try CommsProtocol.result(Array(body.utf8), session: "ltc-comms"), "Accepted malformed fixture \(index)")
        }
    }

    func testRejectsMalformedUTF8AndOutOfBoundsBodySizes() {
        for bytes: [UInt8] in [[0xC0, 0xAF], [0xED, 0xA0, 0x80], [0xF4, 0x90, 0x80, 0x80], [0xFF], []] {
            XCTAssertThrowsError(try CommsProtocol.subscribed(bytes, session: "ltc-comms"))
        }
        XCTAssertThrowsError(try CommsProtocol.subscribed(Array((subscribed + String(repeating: " ", count: 16384)).utf8), session: "ltc-comms"))
    }

    func testSessionUsesUnicodeScalarsAndExactIdentity() throws {
        for session in [String(repeating: "😀", count: 128), #"quote"back\slash"#, "a\u{301}"] {
            var decoder = FrameDecoder()
            let bodies = try decoder.feed(CommsProtocol.subscribe(session: session))
            let request = try XCTUnwrap(bodies.first)
            let ack = String(decoding: request, as: UTF8.self).replacingOccurrences(of: "SUBSCRIBE", with: "SUBSCRIBED")
            try CommsProtocol.subscribed(Array(ack.utf8), session: session)
        }
        for session in ["", String(repeating: "a", count: 129), String(repeating: "a\u{301}", count: 65), "x\n", "\u{0}"] {
            XCTAssertThrowsError(try CommsProtocol.subscribe(session: session))
        }
        let escaped = #"{"v":1,"type":"SUBSCRIBED","session_id":"\ud83d\ude00"}"#
        try CommsProtocol.subscribed(Array(escaped.utf8), session: "😀")
        let normalized = #"{"v":1,"type":"SUBSCRIBED","session_id":"é"}"#
        XCTAssertThrowsError(try CommsProtocol.subscribed(Array(normalized.utf8), session: "e\u{301}"))
    }

    func testSubscribedRejectsExtraFieldsAndWrongMessageType() {
        for body in [subscribed.replacingOccurrences(of: "}", with: #","seq":1}"#), result,
                     subscribed.replacingOccurrences(of: "ltc-comms", with: "other"),
                     subscribed.replacingOccurrences(of: #""v":1"#, with: #""v":1,"\u0076":1"#)] {
            XCTAssertThrowsError(try CommsProtocol.subscribed(Array(body.utf8), session: "ltc-comms"))
        }
    }

    func testFragmentedAndCoalescedFramesPreserveBodies() throws {
        var decoder = FrameDecoder()
        XCTAssertEqual(try decoder.feed([0]), [])
        XCTAssertTrue(decoder.hasPartialFrame)
        XCTAssertEqual(try decoder.feed([0, 0]), [])
        XCTAssertEqual(try decoder.feed([2, 0x7B]), [])
        XCTAssertEqual(decoder.bufferedByteCount, 5)
        XCTAssertEqual(try decoder.feed([0x7D, 0, 0, 0, 1, 0x20, 0, 0]), [[0x7B, 0x7D], [0x20]])
        XCTAssertEqual(decoder.bufferedByteCount, 2)
        XCTAssertEqual(try decoder.feed([0, 1, 0x41]), [[0x41]])
        XCTAssertFalse(decoder.hasPartialFrame)
        XCTAssertNoThrow(try decoder.finish())
    }

    func testFrameBoundaryRejectsInvalidPrefixWithoutResynchronizing() throws {
        for prefix: [UInt8] in [[0, 0, 0, 0], [0, 0, 64, 1], [0xFF, 0xFF, 0xFF, 0xFF]] {
            var decoder = FrameDecoder()
            XCTAssertThrowsError(try decoder.feed(prefix))
            XCTAssertThrowsError(try decoder.feed([0, 0, 0, 1, 0x41]))
        }
        var maximum = FrameDecoder()
        XCTAssertEqual(try maximum.feed([0, 0, 64, 0]), [])
        let body = [UInt8](repeating: 0x20, count: 16384)
        XCTAssertEqual(try maximum.feed(body), [body])
    }

    func testEOFRejectsPartialPrefixAndBody() throws {
        for bytes: [UInt8] in [[0], [0, 0, 0], [0, 0, 0, 2, 0x7B]] {
            var decoder = FrameDecoder()
            _ = try decoder.feed(bytes)
            XCTAssertThrowsError(try decoder.finish())
        }
    }
}
