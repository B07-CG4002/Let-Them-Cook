import Foundation

public enum CommsProtocolError: Error, Equatable {
    case malformedJSON, invalidSchema, invalidSession, invalidFrameLength, incompleteFrame, decoderFailed
}

public struct GestureResult: Equatable, Sendable {
    public let json: String
    public let resultID: String
    public let gesture: String
    public let seq: UInt32
    public let deviceID: UInt32
    public let bootID: UInt32
    public let requestID: UInt32?
}

public enum CommsProtocol {
    public static let maximumFrameBytes = 16_384
    private static let baseFields: Set<String> = ["v", "type", "session_id"]
    private static let resultFields = baseFields.union(["device_id", "boot_id", "seq", "result_id", "gesture", "confidence"])

    /// Returns a complete wire frame, including its four-byte big-endian prefix.
    public static func subscribe(session: String) throws -> [UInt8] {
        try validateSession(session)
        let quoted = session.replacingOccurrences(of: "\\", with: "\\\\").replacingOccurrences(of: "\"", with: "\\\"")
        let body = Array("{\"v\":1,\"type\":\"SUBSCRIBE\",\"session_id\":\"\(quoted)\"}".utf8)
        let length = UInt32(body.count)
        return [UInt8(length >> 24), UInt8((length >> 16) & 255), UInt8((length >> 8) & 255), UInt8(length & 255)] + body
    }

    public static func subscribed(_ body: [UInt8], session: String) throws {
        let (_, fields) = try parse(body)
        try header(fields, kind: "SUBSCRIBED", session: session, allowed: baseFields)
    }

    public static func result(_ body: [UInt8], session: String) throws -> GestureResult {
        let (json, fields) = try parse(body)
        let version = try integer(fields["v"])
        let allowed = version == 2 ? resultFields.union(["request_id"]) : resultFields
        try header(fields, kind: "GESTURE_RESULT", session: session, allowed: allowed, versions: [1, 2])
        let device = try integer(fields["device_id"])
        let boot = try integer(fields["boot_id"])
        let seq = try integer(fields["seq"])
        let resultID = try string(fields["result_id"])
        let gesture = try string(fields["gesture"])
        var requestID: UInt32?
        if version == 2 {
            if case .null = fields["request_id"] { requestID = nil }
            else {
                requestID = try integer(fields["request_id"])
                guard requestID == seq, seq > 0 else { throw CommsProtocolError.invalidSchema }
            }
        }
        let expectedID = (requestID == nil ? "" : "cmd:") + "\(device):\(boot):\(seq)"
        let gestures = ["REST", "FIST", "OPEN", "POINT"]
        guard device == 1 || device == 2,
              resultID == expectedID,
              version == 1 ? gesture == gestures[Int(seq % 4)] : gestures.contains(gesture),
              case .number(let token) = fields["confidence"],
              let confidence = Double(token), confidence.isFinite, confidence == 1 else {
            throw CommsProtocolError.invalidSchema
        }
        return GestureResult(json: json, resultID: resultID, gesture: gesture, seq: seq, deviceID: device, bootID: boot, requestID: requestID)
    }

    private static func validateSession(_ session: String) throws {
        let scalars = session.unicodeScalars
        guard (1...128).contains(scalars.count), scalars.allSatisfy({ $0.value >= 32 }) else {
            throw CommsProtocolError.invalidSession
        }
    }

    private static func parse(_ body: [UInt8]) throws -> (String, [String: JSONAtom]) {
        guard (1...maximumFrameBytes).contains(body.count) else { throw CommsProtocolError.invalidFrameLength }
        // Foundation may strip a leading UTF-8 BOM. Require lossless byte identity.
        guard let json = String(bytes: body, encoding: .utf8), Array(json.utf8) == body else {
            throw CommsProtocolError.malformedJSON
        }
        var parser = FlatJSON(json)
        return (json, try parser.object())
    }

    private static func header(_ fields: [String: JSONAtom], kind: String, session: String, allowed: Set<String>, versions: Set<UInt32> = [1]) throws {
        try validateSession(session)
        guard Set(fields.keys) == allowed, try versions.contains(integer(fields["v"])),
              try string(fields["type"]) == kind,
              // Swift String equality normalizes Unicode; wire session identity does not.
              try string(fields["session_id"]).unicodeScalars.elementsEqual(session.unicodeScalars) else {
            throw CommsProtocolError.invalidSchema
        }
    }

    private static func integer(_ value: JSONAtom?) throws -> UInt32 {
        guard case .number(let token) = value, token.utf8.count <= 10,
              token.utf8.allSatisfy({ (48...57).contains($0) }), let integer = UInt32(token) else {
            throw CommsProtocolError.invalidSchema
        }
        return integer
    }

    private static func string(_ value: JSONAtom?) throws -> String {
        guard case .string(let string) = value else { throw CommsProtocolError.invalidSchema }
        return string
    }
}

/// A stream decoder retains only one incomplete frame (at most 16,388 bytes).
/// Any framing error poisons it; callers must reconnect instead of resynchronizing.
public struct FrameDecoder {
    private var buffer: [UInt8] = []
    private var bodyLength: Int?
    private var failed = false
    public init() {}
    public var hasPartialFrame: Bool { !buffer.isEmpty }
    public var bufferedByteCount: Int { buffer.count }

    public mutating func feed(_ bytes: [UInt8]) throws -> [[UInt8]] {
        guard !failed else { throw CommsProtocolError.decoderFailed }
        var offset = 0
        var complete: [[UInt8]] = []
        while offset < bytes.count {
            let target = bodyLength.map { $0 + 4 } ?? 4
            let count = min(target - buffer.count, bytes.count - offset)
            buffer.append(contentsOf: bytes[offset..<(offset + count)])
            offset += count
            if bodyLength == nil, buffer.count == 4 {
                let length = buffer.reduce(UInt32(0)) { ($0 << 8) | UInt32($1) }
                guard (1...UInt32(CommsProtocol.maximumFrameBytes)).contains(length) else {
                    failed = true
                    buffer.removeAll(keepingCapacity: false)
                    throw CommsProtocolError.invalidFrameLength
                }
                bodyLength = Int(length)
            }
            if let length = bodyLength, buffer.count == length + 4 {
                complete.append(Array(buffer.dropFirst(4)))
                buffer.removeAll(keepingCapacity: true)
                bodyLength = nil
            }
        }
        return complete
    }

    public mutating func finish() throws {
        guard !failed else { throw CommsProtocolError.decoderFailed }
        guard buffer.isEmpty else {
            failed = true
            buffer.removeAll(keepingCapacity: false)
            throw CommsProtocolError.incompleteFrame
        }
    }
}

private enum JSONAtom {
    case string(String)
    case number(String)
    case null
}

/// The schemas are flat. Only v2 request_id permits null; validators reject it
/// in other fields. Lexical numbers preserve the distinction between 42 and 42.0.
private struct FlatJSON {
    private let input: [Unicode.Scalar]
    private var index = 0

    init(_ input: String) { self.input = Array(input.unicodeScalars) }

    mutating func object() throws -> [String: JSONAtom] {
        space()
        try take(123)
        space()
        var fields: [String: JSONAtom] = [:]
        if peek(125) {
            index += 1
        } else {
            while true {
                let key = try string()
                // V2 adds one correlation field. Check decoded keys before insertion.
                guard fields[key] == nil, fields.count < 10 else { throw CommsProtocolError.invalidSchema }
                space()
                try take(58)
                space()
                if peek(110) {
                    for scalar in [UInt32(110), 117, 108, 108] { try take(scalar) }
                    fields[key] = .null
                } else {
                    fields[key] = try peek(34) ? .string(string()) : .number(number())
                }
                space()
                if peek(125) { index += 1; break }
                try take(44)
                space()
            }
        }
        space()
        guard index == input.count else { throw CommsProtocolError.malformedJSON }
        return fields
    }

    private mutating func string() throws -> String {
        try take(34)
        var decoded = String.UnicodeScalarView()
        while index < input.count {
            var scalar = input[index]
            index += 1
            if scalar.value == 34 { return String(decoded) }
            guard scalar.value >= 32 else { throw CommsProtocolError.malformedJSON }
            if scalar.value == 92 {
                guard index < input.count else { throw CommsProtocolError.malformedJSON }
                scalar = input[index]
                index += 1
                switch scalar.value {
                case 34, 92, 47: break
                case 98: scalar = "\u{8}"
                case 102: scalar = "\u{C}"
                case 110: scalar = "\n"
                case 114: scalar = "\r"
                case 116: scalar = "\t"
                case 117:
                    var code = try hexQuad()
                    if (0xD800...0xDBFF).contains(code) {
                        try take(92)
                        try take(117)
                        let low = try hexQuad()
                        guard (0xDC00...0xDFFF).contains(low) else { throw CommsProtocolError.malformedJSON }
                        code = 0x10000 + ((code - 0xD800) << 10) + low - 0xDC00
                    }
                    guard let unicode = Unicode.Scalar(code) else { throw CommsProtocolError.malformedJSON }
                    scalar = unicode
                default: throw CommsProtocolError.malformedJSON
                }
            }
            decoded.append(scalar)
        }
        throw CommsProtocolError.malformedJSON
    }

    private mutating func hexQuad() throws -> UInt32 {
        guard input.count - index >= 4 else { throw CommsProtocolError.malformedJSON }
        var result: UInt32 = 0
        for _ in 0..<4 {
            let code = input[index].value
            let digit: UInt32
            switch code {
            case 48...57: digit = code - 48
            case 65...70: digit = code - 55
            case 97...102: digit = code - 87
            default: throw CommsProtocolError.malformedJSON
            }
            result = (result << 4) | digit
            index += 1
        }
        return result
    }

    private mutating func number() throws -> String {
        let start = index
        if peek(45) { index += 1 }
        if peek(48) {
            index += 1
        } else {
            guard index < input.count, (49...57).contains(input[index].value) else { throw CommsProtocolError.malformedJSON }
            digits()
        }
        if peek(46) {
            index += 1
            let fraction = index
            digits()
            guard index > fraction else { throw CommsProtocolError.malformedJSON }
        }
        if peek(101) || peek(69) {
            index += 1
            if peek(43) || peek(45) { index += 1 }
            let exponent = index
            digits()
            guard index > exponent else { throw CommsProtocolError.malformedJSON }
        }
        let token = String(String.UnicodeScalarView(input[start..<index]))
        guard let value = Double(token), value.isFinite else { throw CommsProtocolError.malformedJSON }
        return token
    }

    private mutating func digits() {
        while index < input.count, (48...57).contains(input[index].value) { index += 1 }
    }

    private mutating func space() {
        while index < input.count, [9, 10, 13, 32].contains(input[index].value) { index += 1 }
    }

    private func peek(_ value: UInt32) -> Bool { index < input.count && input[index].value == value }

    private mutating func take(_ value: UInt32) throws {
        guard peek(value) else { throw CommsProtocolError.malformedJSON }
        index += 1
    }
}
