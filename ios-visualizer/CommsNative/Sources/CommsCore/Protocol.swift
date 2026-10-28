import Foundation

public enum CommsProtocolError: Error, Equatable {
    case malformedJSON, invalidSchema, invalidSession, invalidFrameLength, incompleteFrame, decoderFailed
}

public struct GestureResult: Equatable, Sendable {
    public let json: String
    public let resultID: String
    public let gesture: String
    public let confidence: Double
    public let seq: UInt32
    public let deviceID: UInt32
    public let bootID: UInt32
    public let requestID: UInt32?
}

extension GestureResult {
    /// The object CommsNextResult hands to the game, keys in this order. The parser
    /// limits result_id to digits, colons and "cmd:", and gesture to [A-Za-z0-9_-],
    /// so neither needs escaping; a finite Double's description is a JSON number.
    public var nextResultJSON: String {
        "{\"result_id\":\"\(resultID)\",\"gesture\":\"\(gesture)\",\"confidence\":\(confidence),\"device_id\":\(deviceID)}"
    }
}

/// One HAND_EVENT: Wen Ree's per-hand event envelope (common/hand_contract.py,
/// src/hand_events.py). device_id 1 is the LEFT hand, 2 the RIGHT hand; each hand
/// counts its own event_seq within one worker_epoch.
public struct HandEvent: Equatable, Sendable {
    public enum Kind: String, CaseIterable, Sendable {
        case pulse
        case stateStart = "state_start"
        case stateRefresh = "state_refresh"
        case stateEnd = "state_end"
        case idle
        case reset
    }
    /// The glove sample the event came from. Two events may name the same sample
    /// (a CHOP-to-POURING transition ends one state and starts another on it).
    public struct Source: Equatable, Sendable {
        public let bootID: UInt32
        public let seq: UInt32
        public let uptimeMS: UInt32
    }
    public let json: String
    public let session: String
    public let workerEpoch: String
    public let deviceID: UInt32
    public let eventSeq: UInt64
    public let eventID: String
    public let kind: Kind
    /// Null only for reset.
    public let gesture: String?
    /// Null for state_end and reset.
    public let confidence: Double?
    /// Null only for some resets.
    public let source: Source?
    /// The state a state_start begins (its own event_id), or a state_refresh or
    /// state_end names; null for the other kinds.
    public let stateID: String?
    /// The worker's lease on state_start and state_refresh, 0 on the other kinds.
    public let leaseMS: UInt32
    public let reason: String
}

extension HandEvent {
    /// The event CommsNextHandEvent hands to the game: all fifteen fields in the
    /// envelope's order, without spaces. The parser limits worker_epoch, event_id and
    /// state_id to hex digits, digits and colons, and kind, gesture and reason to fixed
    /// ASCII names, so none needs escaping; session_id has no control characters
    /// (validateSession), so only quotes and backslashes are escaped, as in subscribe().
    public var compactJSON: String {
        let session = self.session.replacingOccurrences(of: "\\", with: "\\\\").replacingOccurrences(of: "\"", with: "\\\"")
        let gesture = self.gesture.map { "\"\($0)\"" } ?? "null"
        let confidence = self.confidence.map { "\($0)" } ?? "null"
        let source = self.source.map { "{\"boot_id\":\($0.bootID),\"seq\":\($0.seq),\"uptime_ms\":\($0.uptimeMS)}" } ?? "null"
        let state = stateID.map { "\"\($0)\"" } ?? "null"
        return "{\"v\":4,\"type\":\"HAND_EVENT\",\"event_format\":1,\"session_id\":\"\(session)\",\"worker_epoch\":\"\(workerEpoch)\","
            + "\"device_id\":\(deviceID),\"event_seq\":\(eventSeq),\"event_id\":\"\(eventID)\",\"kind\":\"\(kind.rawValue)\","
            + "\"gesture\":\(gesture),\"confidence\":\(confidence),\"source\":\(source),\"state_id\":\(state),"
            + "\"lease_ms\":\(leaseMS),\"reason\":\"\(reason)\"}"
    }
}

/// What CommsProtocol.subscribedFrame found in one frame of a version 2 subscription.
public enum SubscribedFrame: Equatable, Sendable {
    case result(GestureResult)
    case handEvent(HandEvent)
    /// A frame whose type is HAND_EVENT but which failed validation.
    case invalidHandEvent(CommsProtocolError)
}

public enum CommsProtocol {
    public static let maximumFrameBytes = 16_384
    public static let maximumGestureLength = 32
    /// The largest event_seq, 2^53 - 1: exactly representable as a JavaScript number.
    public static let maximumEventSeq: UInt64 = 9_007_199_254_740_991
    private static let baseFields: Set<String> = ["v", "type", "session_id"]
    // Protocol version 3: request_id is always present (null except for command echoes).
    private static let resultFields = baseFields.union(["device_id", "boot_id", "seq", "request_id", "result_id", "gesture", "confidence"])
    // Subscription version 2 asks for hand events as well; SUBSCRIBED echoes event_format.
    private static let subscribedHandFields = baseFields.union(["event_format"])
    // Wen Ree's HAND_EVENT envelope (common/hand_contract.py, _EVENT_FIELDS): fifteen keys,
    // one of them the nested source object of exactly three keys.
    private static let handEventFields = baseFields.union(["event_format", "worker_epoch", "device_id", "event_seq", "event_id",
                                                           "kind", "gesture", "confidence", "source", "state_id", "lease_ms", "reason"])
    private static let sourceFields: Set<String> = ["boot_id", "seq", "uptime_ms"]
    private static let pulseGestures = ["PICKUP", "DROP", "THROW"]
    private static let ongoingGestures = ["CHOP", "WASH", "POURING"]
    private static let endReasons = ["confirmed_transition", "prediction_timeout", "source_reset", "connection_reset",
                                     "worker_disconnect", "manual_reset"]

    /// Returns a complete wire frame, including its four-byte big-endian prefix.
    /// Version 1 asks for GESTURE_RESULT frames only; version 2 (handEvents) asks
    /// for HAND_EVENT frames as well, in the same ordered stream.
    public static func subscribe(session: String, handEvents: Bool = false) throws -> [UInt8] {
        try validateSession(session)
        let quoted = session.replacingOccurrences(of: "\\", with: "\\\\").replacingOccurrences(of: "\"", with: "\\\"")
        let text = handEvents ? "{\"v\":2,\"type\":\"SUBSCRIBE\",\"session_id\":\"\(quoted)\",\"event_format\":1}"
                              : "{\"v\":1,\"type\":\"SUBSCRIBE\",\"session_id\":\"\(quoted)\"}"
        let body = Array(text.utf8)
        let length = UInt32(body.count)
        return [UInt8(length >> 24), UInt8((length >> 16) & 255), UInt8((length >> 8) & 255), UInt8(length & 255)] + body
    }

    /// SUBSCRIBED must answer the SUBSCRIBE that was sent: version 1 with exactly the
    /// three base fields, or version 2 with event_format 1 as well.
    public static func subscribed(_ body: [UInt8], session: String, handEvents: Bool = false) throws {
        let (_, fields) = try parse(body)
        if handEvents {
            try header(fields, kind: "SUBSCRIBED", session: session, allowed: subscribedHandFields, versions: [2])
            guard try integer(fields["event_format"]) == 1 else { throw CommsProtocolError.invalidSchema }
        } else {
            try header(fields, kind: "SUBSCRIBED", session: session, allowed: baseFields)
        }
    }

    /// Accepts only a version 3 GESTURE_RESULT. SUBSCRIBE and SUBSCRIBED are version 1,
    /// or version 2 for a subscription with hand events.
    public static func result(_ body: [UInt8], session: String) throws -> GestureResult {
        let (json, fields) = try parse(body)
        try header(fields, kind: "GESTURE_RESULT", session: session, allowed: resultFields, versions: [3])
        let device = try integer(fields["device_id"])
        let boot = try integer(fields["boot_id"])
        let seq = try integer(fields["seq"])
        let resultID = try string(fields["result_id"])
        let gesture = try string(fields["gesture"])
        var requestID: UInt32?
        if case .null = fields["request_id"] { requestID = nil }
        else {
            requestID = try integer(fields["request_id"])
            guard requestID == seq, seq > 0 else { throw CommsProtocolError.invalidSchema }
        }
        let expectedID = (requestID == nil ? "" : "cmd:") + "\(device):\(boot):\(seq)"
        // Confidence comes from the lexical number token; FlatJSON never turns
        // true, false, a string or null into a number.
        guard device == 1 || device == 2,
              resultID.unicodeScalars.elementsEqual(expectedID.unicodeScalars),
              validGesture(gesture),
              case .number(let token) = fields["confidence"],
              let confidence = Double(token), confidence.isFinite, (0...1).contains(confidence) else {
            throw CommsProtocolError.invalidSchema
        }
        return GestureResult(json: json, resultID: resultID, gesture: gesture, confidence: confidence, seq: seq, deviceID: device, bootID: boot, requestID: requestID)
    }

    /// One frame after a version 2 SUBSCRIBED. A GESTURE_RESULT goes through result()
    /// unchanged, and every frame that is not a HAND_EVENT fails there exactly as on a
    /// version 1 subscription. A frame whose top-level type is HAND_EVENT never throws:
    /// an invalid one comes back as invalidHandEvent, to be counted and dropped while the
    /// connection stays up (the framing is intact, and results must not be lost to it).
    /// The type counts once the strict parse has read it or, when that parse stopped
    /// earlier, when the frame is well-formed JSON (skimType); other bytes still fail.
    public static func subscribedFrame(_ body: [UInt8], session: String) throws -> SubscribedFrame {
        if (1...maximumFrameBytes).contains(body.count), let json = String(bytes: body, encoding: .utf8), Array(json.utf8) == body {
            var parser = FlatJSON(json, maximumKeys: handEventFields.count, nested: "source", nestedKeys: sourceFields.count)
            do {
                let fields = try parser.object()
                if case .string(let type) = fields["type"], exact(type, "HAND_EVENT") {
                    do { return .handEvent(try handEvent(json: json, fields: fields, session: session)) }
                    catch { return .invalidHandEvent(error as? CommsProtocolError ?? .invalidSchema) }
                }
            } catch {
                // The parse stopped after the top-level type was read: still a HAND_EVENT.
                if let type = parser.topLevelType, exact(type, "HAND_EVENT") {
                    return .invalidHandEvent(error as? CommsProtocolError ?? .malformedJSON)
                }
                // It stopped before the type (a non-flat value, too many keys, another key
                // order): a well-formed object whose top-level type is HAND_EVENT is still one.
                if parser.topLevelType == nil {
                    var skim = FlatJSON(json)
                    if let type = skim.skimType(), exact(type, "HAND_EVENT") {
                        return .invalidHandEvent(error as? CommsProtocolError ?? .malformedJSON)
                    }
                }
            }
        }
        return .result(try result(body, session: session))
    }

    /// Accepts only a HAND_EVENT of version 4, event_format 1, with all fifteen fields of
    /// Wen Ree's envelope checked as his validate_event checks them, except what needs the
    /// worker's hello: the confidence threshold and the exact lease are the service's checks.
    public static func handEvent(_ body: [UInt8], session: String) throws -> HandEvent {
        let (json, fields) = try parse(body, maximumKeys: handEventFields.count, nested: "source", nestedKeys: sourceFields.count)
        return try handEvent(json: json, fields: fields, session: session)
    }

    private static func handEvent(json: String, fields: [String: JSONAtom], session: String) throws -> HandEvent {
        try header(fields, kind: "HAND_EVENT", session: session, allowed: handEventFields, versions: [4])
        let epoch = try string(fields["worker_epoch"])
        let device = try integer(fields["device_id"])
        let sequence = try counter(fields["event_seq"])
        let identity = try string(fields["event_id"])
        let kindText = try string(fields["kind"])
        let reasonText = try string(fields["reason"])
        let lease = try integer(fields["lease_ms"])
        guard try integer(fields["event_format"]) == 1,
              // worker_epoch: exactly 32 lowercase hexadecimal digits.
              epoch.unicodeScalars.count == 32,
              epoch.unicodeScalars.allSatisfy({ (48...57).contains($0.value) || (97...102).contains($0.value) }),
              device == 1 || device == 2, (1...maximumEventSeq).contains(sequence),
              exact(identity, "\(epoch):\(device):\(sequence)"),
              let kind = HandEvent.Kind.allCases.first(where: { exact(kindText, $0.rawValue) }) else {
            throw CommsProtocolError.invalidSchema
        }
        let prediction = kind == .pulse || kind == .stateStart || kind == .stateRefresh || kind == .idle
        let ongoing = kind == .stateStart || kind == .stateRefresh || kind == .stateEnd

        // gesture: pulse PICKUP, DROP or THROW; the three state kinds CHOP, WASH or POURING;
        // idle IDLE; reset null. The stored value is the constant, not the received text.
        var gesture: String?
        if kind == .reset {
            guard case .null = fields["gesture"] else { throw CommsProtocolError.invalidSchema }
        } else {
            let text = try string(fields["gesture"])
            let allowed = kind == .pulse ? pulseGestures : kind == .idle ? ["IDLE"] : ongoingGestures
            gesture = allowed.first(where: { exact(text, $0) })
            guard gesture != nil else { throw CommsProtocolError.invalidSchema }
        }

        // confidence: a number in [0, 1] for a prediction, null for state_end and reset.
        var confidence: Double?
        if prediction {
            guard case .number(let token) = fields["confidence"], let value = Double(token), value.isFinite, (0...1).contains(value) else {
                throw CommsProtocolError.invalidSchema
            }
            confidence = value
        } else {
            guard case .null = fields["confidence"] else { throw CommsProtocolError.invalidSchema }
        }

        // source: exactly boot_id, seq and uptime_ms, each a uint32. Only reset may carry
        // null (validate_event accepts a reset with or without a source).
        var source: HandEvent.Source?
        if case .object(let inner) = fields["source"] {
            guard Set(inner.keys) == sourceFields else { throw CommsProtocolError.invalidSchema }
            source = HandEvent.Source(bootID: try integer(inner["boot_id"]), seq: try integer(inner["seq"]), uptimeMS: try integer(inner["uptime_ms"]))
        } else {
            guard kind == .reset, case .null = fields["source"] else { throw CommsProtocolError.invalidSchema }
        }

        // state_id: a start names itself; a refresh or an end names "<epoch>:<device>:<n>"
        // with n a decimal 1...event_seq without leading zeros; every other kind null.
        var stateID: String?
        if ongoing {
            let text = try string(fields["state_id"])
            let prefix = Array("\(epoch):\(device):".unicodeScalars)
            let scalars = Array(text.unicodeScalars)
            guard scalars.count > prefix.count, scalars.count <= prefix.count + 16, scalars.starts(with: prefix) else {
                throw CommsProtocolError.invalidSchema
            }
            let digits = scalars[prefix.count...]
            guard digits.allSatisfy({ (48...57).contains($0.value) }), digits.first?.value != 48,
                  let number = UInt64(String(String.UnicodeScalarView(digits))), (1...sequence).contains(number),
                  kind != .stateStart || number == sequence else {
                throw CommsProtocolError.invalidSchema
            }
            stateID = "\(epoch):\(device):\(number)"
        } else {
            guard case .null = fields["state_id"] else { throw CommsProtocolError.invalidSchema }
        }

        // lease_ms: 100...2000 on state_start and state_refresh (the hello fixes the exact
        // value; the service checks it), 0 on every other kind.
        // reason: "confirmed" for a prediction, one of the six end reasons otherwise.
        let reason = prediction ? (exact(reasonText, "confirmed") ? "confirmed" : nil) : endReasons.first(where: { exact(reasonText, $0) })
        guard kind == .stateStart || kind == .stateRefresh ? (100...2000).contains(lease) : lease == 0, let reason else {
            throw CommsProtocolError.invalidSchema
        }
        return HandEvent(json: json, session: session, workerEpoch: epoch, deviceID: device, eventSeq: sequence, eventID: identity,
                         kind: kind, gesture: gesture, confidence: confidence, source: source, stateID: stateID, leaseMS: lease, reason: reason)
    }

    /// 1...32 Unicode scalars, each an ASCII letter, digit, "_" or "-": the same rule
    /// as the Python and C# receivers. Scalars, not Characters, so a composed or
    /// combining sequence never passes as one character.
    private static func validGesture(_ gesture: String) -> Bool {
        let scalars = gesture.unicodeScalars
        return (1...maximumGestureLength).contains(scalars.count) && scalars.allSatisfy { scalar in
            switch scalar.value {
            case 48...57, 65...90, 97...122, 45, 95: return true
            default: return false
            }
        }
    }

    private static func validateSession(_ session: String) throws {
        let scalars = session.unicodeScalars
        guard (1...128).contains(scalars.count), scalars.allSatisfy({ $0.value >= 32 }) else {
            throw CommsProtocolError.invalidSession
        }
    }

    private static func parse(_ body: [UInt8], maximumKeys: Int = 10, nested: String? = nil, nestedKeys: Int = 0) throws -> (String, [String: JSONAtom]) {
        guard (1...maximumFrameBytes).contains(body.count) else { throw CommsProtocolError.invalidFrameLength }
        // Foundation may strip a leading UTF-8 BOM. Require lossless byte identity.
        guard let json = String(bytes: body, encoding: .utf8), Array(json.utf8) == body else {
            throw CommsProtocolError.malformedJSON
        }
        var parser = FlatJSON(json, maximumKeys: maximumKeys, nested: nested, nestedKeys: nestedKeys)
        return (json, try parser.object())
    }

    /// Swift String equality normalizes Unicode (U+212A KELVIN SIGN equals "K");
    /// wire identity does not.
    private static func exact(_ value: String, _ expected: String) -> Bool {
        value.unicodeScalars.elementsEqual(expected.unicodeScalars)
    }

    /// event_seq: 1...16 ASCII digits, at most 2^53 - 1 (the caller checks the lower bound).
    private static func counter(_ value: JSONAtom?) throws -> UInt64 {
        guard case .number(let token) = value, token.utf8.count <= 16,
              token.utf8.allSatisfy({ (48...57).contains($0) }), let counter = UInt64(token), counter <= maximumEventSeq else {
            throw CommsProtocolError.invalidSchema
        }
        return counter
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
    // Only the value of the one key a parser is built to nest (a HAND_EVENT's source).
    case object([String: JSONAtom])
}

/// The schemas are flat. Only request_id permits null; validators reject it
/// in other fields. Lexical numbers preserve the distinction between 42 and 42.0.
/// The one exception is the HAND_EVENT parser: it is built with nested "source",
/// whose value may be one flat object of at most nestedKeys keys. No other key,
/// and nothing inside that object, may hold an object; arrays are never accepted.
private struct FlatJSON {
    private let input: [Unicode.Scalar]
    private var index = 0
    private let maximumKeys: Int
    private let nested: String?
    private let nestedKeys: Int
    /// The top-level "type" string once it has been read, kept when a later part of
    /// the object fails: it tells an invalid HAND_EVENT from any other bad frame.
    private(set) var topLevelType: String?

    init(_ input: String, maximumKeys: Int = 10, nested: String? = nil, nestedKeys: Int = 0) {
        self.input = Array(input.unicodeScalars)
        self.maximumKeys = maximumKeys; self.nested = nested; self.nestedKeys = nestedKeys
    }

    mutating func object() throws -> [String: JSONAtom] {
        space()
        let fields = try members(maximumKeys: maximumKeys, topLevel: true)
        space()
        guard index == input.count else { throw CommsProtocolError.malformedJSON }
        return fields
    }

    private mutating func members(maximumKeys: Int, topLevel: Bool) throws -> [String: JSONAtom] {
        try take(123)
        space()
        var fields: [String: JSONAtom] = [:]
        if peek(125) {
            index += 1
        } else {
            while true {
                let key = try string()
                // A version 3 GESTURE_RESULT has exactly ten keys, a HAND_EVENT fifteen and
                // its source three. Check decoded keys before insertion.
                guard fields[key] == nil, fields.count < maximumKeys else { throw CommsProtocolError.invalidSchema }
                space()
                try take(58)
                space()
                if peek(110) {
                    for scalar in [UInt32(110), 117, 108, 108] { try take(scalar) }
                    fields[key] = .null
                } else if topLevel, peek(123), let name = nested, key.unicodeScalars.elementsEqual(name.unicodeScalars) {
                    fields[key] = .object(try members(maximumKeys: nestedKeys, topLevel: false))
                } else {
                    fields[key] = try peek(34) ? .string(string()) : .number(number())
                }
                if topLevel, key.unicodeScalars.elementsEqual("type".unicodeScalars), case .string(let type)? = fields[key] {
                    topLevelType = type
                }
                space()
                if peek(125) { index += 1; break }
                try take(44)
                space()
            }
        }
        return fields
    }

    /// Only for telling a HAND_EVENT the strict parse stopped on from any other bad frame:
    /// the top-level "type" string of a well-formed JSON object (any values, unique keys,
    /// at most 64 levels of nesting, nothing after it), or nil.
    mutating func skimType() -> String? {
        do {
            space()
            let type = try skimObject(depth: 1, topLevel: true)
            space()
            return index == input.count ? type : nil
        } catch { return nil }
    }

    private mutating func skimObject(depth: Int, topLevel: Bool) throws -> String? {
        guard depth <= 64 else { throw CommsProtocolError.malformedJSON }
        try take(123)
        space()
        var keys: Set<String> = []
        var type: String?
        if peek(125) { index += 1; return nil }
        while true {
            let key = try string()
            guard keys.insert(key).inserted else { throw CommsProtocolError.invalidSchema }
            space()
            try take(58)
            space()
            if topLevel, key.unicodeScalars.elementsEqual("type".unicodeScalars), peek(34) {
                type = try string()
            } else {
                try skimValue(depth: depth + 1)
            }
            space()
            if peek(125) { index += 1; return type }
            try take(44)
            space()
        }
    }

    private mutating func skimValue(depth: Int) throws {
        if peek(123) {
            _ = try skimObject(depth: depth, topLevel: false)
        } else if peek(91) {
            guard depth <= 64 else { throw CommsProtocolError.malformedJSON }
            index += 1
            space()
            if peek(93) { index += 1; return }
            while true {
                try skimValue(depth: depth + 1)
                space()
                if peek(93) { index += 1; return }
                try take(44)
                space()
            }
        } else if peek(34) {
            _ = try string()
        } else if peek(116) {
            for scalar in [UInt32(116), 114, 117, 101] { try take(scalar) }
        } else if peek(102) {
            for scalar in [UInt32(102), 97, 108, 115, 101] { try take(scalar) }
        } else if peek(110) {
            for scalar in [UInt32(110), 117, 108, 108] { try take(scalar) }
        } else {
            _ = try number(finite: false)
        }
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

    /// finite: false only for skimType, where 1e999 is still well-formed JSON.
    private mutating func number(finite: Bool = true) throws -> String {
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
        guard let value = Double(token), value.isFinite || !finite else { throw CommsProtocolError.malformedJSON }
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
