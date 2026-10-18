import Foundation
import XCTest
@testable import CommsCore

final class ProtocolTests: XCTestCase {
    private let subscribed = #"{"v":1,"type":"SUBSCRIBED","session_id":"ltc-comms"}"#
    private let result = #"{"v":3,"type":"GESTURE_RESULT","session_id":"ltc-comms","device_id":1,"boot_id":7,"seq":42,"request_id":null,"result_id":"1:7:42","gesture":"CHOP","confidence":0.87}"#

    /// Every case edits a substring that must exist, so a fixture change cannot
    /// silently turn a rejection test into a test of the unchanged valid body.
    private func edit(_ body: String, _ old: String, _ new: String, file: StaticString = #filePath, line: UInt = #line) -> String {
        XCTAssertTrue(body.contains(old), "Fixture does not contain \(old)", file: file, line: line)
        return body.replacingOccurrences(of: old, with: new)
    }

    func testSubscribeEmitsOneLengthPrefixedRequest() throws {
        let expected = Array(#"{"v":1,"type":"SUBSCRIBE","session_id":"ltc-comms"}"#.utf8)
        XCTAssertEqual(try CommsProtocol.subscribe(session: "ltc-comms"), [0, 0, 0, 51] + expected)
    }

    func testLiteralSubscribedAndModelResult() throws {
        try CommsProtocol.subscribed(Array(subscribed.utf8), session: "ltc-comms")
        let message = try CommsProtocol.result(Array(result.utf8), session: "ltc-comms")
        XCTAssertEqual(message.resultID, "1:7:42")
        XCTAssertEqual(message.gesture, "CHOP")
        XCTAssertEqual(message.confidence, 0.87)
        XCTAssertEqual(message.seq, 42)
        XCTAssertEqual(message.deviceID, 1)
        XCTAssertEqual(message.bootID, 7)
        XCTAssertNil(message.requestID)
        XCTAssertEqual(message.json, result)
    }

    func testModelLabelsAndCommandIdentityPreserveCorrelation() throws {
        let labels = ["PICKUPHOLD", "PLACEDOWN", "CHOP", "WASH", "THROW", "FIREEXT", "REST", "FIST", "OPEN", "POINT",
                      "a", "idle_2", "left-hand", "0", "_", "-", String(repeating: "Z", count: 32)]
        for label in labels {
            let message = try CommsProtocol.result(Array(edit(result, #""gesture":"CHOP""#, "\"gesture\":\"\(label)\"").utf8), session: "ltc-comms")
            XCTAssertEqual(message.gesture, label)
            XCTAssertNil(message.requestID)
        }
        let command = edit(edit(result, #""request_id":null"#, #""request_id":42"#), #""1:7:42""#, #""cmd:1:7:42""#)
        let parsed = try CommsProtocol.result(Array(command.utf8), session: "ltc-comms")
        XCTAssertEqual(parsed.requestID, 42)
        XCTAssertEqual(parsed.resultID, "cmd:1:7:42")
        for request in ["true", "-1", "0", "42.0", "4294967296", "41", #""42""#] {
            XCTAssertThrowsError(try CommsProtocol.result(Array(edit(command, #""request_id":42"#, "\"request_id\":\(request)").utf8), session: "ltc-comms"), "Accepted request_id \(request)")
        }
        // A command needs seq > 0: request_id 0 equal to seq 0 is still refused.
        let zero = edit(edit(edit(command, #""seq":42"#, #""seq":0"#), #""request_id":42"#, #""request_id":0"#), #""cmd:1:7:42""#, #""cmd:1:7:0""#)
        XCTAssertThrowsError(try CommsProtocol.result(Array(zero.utf8), session: "ltc-comms"))
        for invalid in [edit(result, #","request_id":null"#, ""),
                        edit(result, #""request_id":null"#, #""request_id":null,"request_id":null"#),
                        edit(result, #""1:7:42""#, #""cmd:1:7:42""#),
                        edit(result, #""request_id":null"#, #""request_id":42"#),
                        edit(result, #""v":3"#, #""v":1"#),
                        edit(result, #""v":3"#, #""v":2"#),
                        edit(result, #""v":3"#, #""v":4"#)] {
            XCTAssertThrowsError(try CommsProtocol.result(Array(invalid.utf8), session: "ltc-comms"), "Accepted \(invalid)")
        }
    }

    func testAcceptsExactUInt32EndpointsAndEveryConfidenceInRange() throws {
        let literals = [
            #"{"v":3,"type":"GESTURE_RESULT","session_id":"ltc-comms","device_id":2,"boot_id":0,"seq":0,"request_id":null,"result_id":"2:0:0","gesture":"REST","confidence":1}"#,
            #"{"v":3,"type":"GESTURE_RESULT","session_id":"ltc-comms","device_id":1,"boot_id":4294967295,"seq":1,"request_id":null,"result_id":"1:4294967295:1","gesture":"FIST","confidence":1e0}"#,
            #"{"v":3,"type":"GESTURE_RESULT","session_id":"ltc-comms","device_id":2,"boot_id":4294967295,"seq":4294967295,"request_id":4294967295,"result_id":"cmd:2:4294967295:4294967295","gesture":"POINT","confidence":1.00}"#,
        ]
        for (body, expected) in zip(literals, [UInt32(0), 1, UInt32.max]) {
            XCTAssertEqual(try CommsProtocol.result(Array(body.utf8), session: "ltc-comms").seq, expected)
        }
        let confidences: [(String, Double)] = [("0", 0), ("0.0", 0), ("-0", 0), ("1", 1), ("1.0", 1), ("100e-2", 1),
                                               ("0.93", 0.93), ("1e-3", 0.001), ("5E-1", 0.5), ("0.999999", 0.999999)]
        for (token, value) in confidences {
            let message = try CommsProtocol.result(Array(edit(result, #""confidence":0.87"#, "\"confidence\":\(token)").utf8), session: "ltc-comms")
            XCTAssertEqual(message.confidence, value, "confidence \(token)")
        }
    }

    func testRejectsWrongTypesSchemaSessionLabelAndConfidence() {
        let replacements = [
            (#""v":3"#, #""v":true"#),
            (#""v":3"#, #""v":3.0"#),
            (#""v":3"#, #""v":"3""#),
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
            (#""confidence":0.87"#, #""confidence":true"#),
            (#""confidence":0.87"#, #""confidence":false"#),
            (#""confidence":0.87"#, #""confidence":"0.87""#),
            (#""confidence":0.87"#, #""confidence":null"#),
            (#""confidence":0.87"#, #""confidence":{}"#),
            (#""confidence":0.87"#, #""confidence":[]"#),
            (#""confidence":0.87"#, #""confidence":-0.01"#),
            (#""confidence":0.87"#, #""confidence":-1"#),
            (#""confidence":0.87"#, #""confidence":1.0000001"#),
            (#""confidence":0.87"#, #""confidence":2"#),
            (#""confidence":0.87"#, #""confidence":1e999"#),
            (#""confidence":0.87"#, #""confidence":-1e999"#),
            (#""confidence":0.87"#, #""confidence":NaN"#),
            (#""confidence":0.87"#, #""confidence":Infinity"#),
            (#""gesture":"CHOP""#, #""gesture":"""#),
            (#""gesture":"CHOP""#, "\"gesture\":\"\(String(repeating: "A", count: 33))\""),
            (#""gesture":"CHOP""#, #""gesture":"CH OP""#),
            (#""gesture":"CHOP""#, #""gesture":"CH.OP""#),
            (#""gesture":"CHOP""#, #""gesture":"CH:OP""#),
            (#""gesture":"CHOP""#, #""gesture":"CH\"OP""#),
            (#""gesture":"CHOP""#, #""gesture":"CH\\OP""#),
            (#""gesture":"CHOP""#, #""gesture":"CHOP\n""#),
            (#""gesture":"CHOP""#, #""gesture":"CHOP\u0000""#),
            (#""gesture":"CHOP""#, #""gesture":"\u00C9""#),
            (#""gesture":"CHOP""#, "\"gesture\":\"CAF\u{C9}\""),
            // One Character but two scalars: a composed check would see four letters.
            (#""gesture":"CHOP""#, "\"gesture\":\"CAFE\u{301}\""),
            (#""gesture":"CHOP""#, "\"gesture\":\"\u{FF23}\u{FF28}\u{FF2F}\u{FF30}\""),
            (#""gesture":"CHOP""#, #""gesture":7"#),
            (#""gesture":"CHOP""#, #""gesture":null"#),
            (#""result_id":"1:7:42""#, #""result_id":"01:7:42""#),
            (#""result_id":"1:7:42""#, #""result_id":"1:7:43""#),
            (#""result_id":"1:7:42""#, #""result_id":"1:7:42 ""#),
            (#""type":"GESTURE_RESULT""#, #""type":"SUBSCRIBED""#),
            (#""session_id":"ltc-comms""#, #""session_id":"other""#),
            (#""confidence":0.87"#, #""confidence":0.87,"extra":1"#),
            (#""confidence":0.87"#, #""score":0.87"#),
        ]
        for (old, new) in replacements {
            XCTAssertThrowsError(try CommsProtocol.result(Array(edit(result, old, new).utf8), session: "ltc-comms"), "Accepted \(new)")
        }
    }

    func testRejectsDuplicateDecodedKeysAndMalformedJSON() {
        let bodies = [
            edit(result, #""seq":42"#, #""seq":42,"seq":42"#),
            edit(result, #""seq":42"#, #""seq":42,"\u0073eq":42"#),
            edit(result, #""seq":42"#, #""seq":042"#),
            edit(result, #""seq":42"#, #""seq":+42"#),
            edit(result, #""confidence":0.87"#, #""confidence":1."#),
            edit(result, #""confidence":0.87"#, #""confidence":1e"#),
            edit(result, #""confidence":0.87"#, #""confidence":.1"#),
            result + "{}", "[" + result + "]", String(result.dropLast()) + ",}",
            edit(result, "CHOP", #"\q"#),
            edit(result, "CHOP", #"\uD800"#),
            edit(result, "CHOP", #"\uDC00"#),
            edit(result, "CHOP", #"\uD800\u0041"#),
            edit(result, "CHOP", "CH\nOP"),
            "\u{FEFF}" + result,
            String(repeating: "[", count: 16000),
        ]
        for (index, body) in bodies.enumerated() {
            XCTAssertThrowsError(try CommsProtocol.result(Array(body.utf8), session: "ltc-comms"), "Accepted malformed fixture \(index)")
        }
    }

    func testNextResultJSONKeepsKeyOrderAndReceivedConfidence() throws {
        let message = try CommsProtocol.result(Array(result.utf8), session: "ltc-comms")
        XCTAssertEqual(message.nextResultJSON, #"{"result_id":"1:7:42","gesture":"CHOP","confidence":0.87,"device_id":1}"#)
        for (token, printed) in [("1", "1.0"), ("0", "0.0"), ("0.93", "0.93")] {
            let parsed = try CommsProtocol.result(Array(edit(result, #""confidence":0.87"#, "\"confidence\":\(token)").utf8), session: "ltc-comms")
            XCTAssertEqual(parsed.nextResultJSON, "{\"result_id\":\"1:7:42\",\"gesture\":\"CHOP\",\"confidence\":\(printed),\"device_id\":1}")
        }
        let command = edit(edit(edit(result, #""request_id":null"#, #""request_id":42"#), #""1:7:42""#, #""cmd:1:7:42""#), #""device_id":1"#, #""device_id":2"#)
        let echo = try CommsProtocol.result(Array(edit(command, #""cmd:1:7:42""#, #""cmd:2:7:42""#).utf8), session: "ltc-comms")
        XCTAssertEqual(echo.nextResultJSON, #"{"result_id":"cmd:2:7:42","gesture":"CHOP","confidence":0.87,"device_id":2}"#)
        // Small values print in exponent form; the text must still be JSON and read back exactly.
        for token in ["1e-5", "0.000123", "0.1", "0.30000000000000004"] {
            let parsed = try CommsProtocol.result(Array(edit(result, #""confidence":0.87"#, "\"confidence\":\(token)").utf8), session: "ltc-comms")
            let text = parsed.nextResultJSON
            let object = try XCTUnwrap(JSONSerialization.jsonObject(with: Data(text.utf8)) as? [String: Any], text)
            XCTAssertEqual(object.count, 4)
            let printed = try XCTUnwrap(text.components(separatedBy: #""confidence":"#).last?.components(separatedBy: ",").first)
            XCTAssertEqual(Double(printed), parsed.confidence, text)
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

    func testSubscribedRejectsExtraFieldsWrongVersionAndWrongMessageType() {
        // SUBSCRIBED never carried the packet version; it stays at version 1.
        for body in [subscribed.replacingOccurrences(of: "}", with: #","seq":1}"#), result,
                     edit(subscribed, "ltc-comms", "other"),
                     edit(subscribed, #""v":1"#, #""v":3"#),
                     edit(subscribed, #""v":1"#, #""v":1,"\u0076":1"#)] {
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
