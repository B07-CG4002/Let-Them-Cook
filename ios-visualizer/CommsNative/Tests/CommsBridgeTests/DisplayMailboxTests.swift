import XCTest
import CommsCore
@testable import CommsBridge

final class DisplayMailboxTests: XCTestCase {
    func testCopiesOnlyChangedDisplayAndNeverOverrunsBuffer() throws {
        let state = DisplayState()
        let mailbox = DisplayMailbox(state: state)
        var bytes = [CChar](repeating: 42, count: 2048)
        let first = bytes.withUnsafeMutableBufferPointer { mailbox.copy(into: $0.baseAddress, capacity: Int32($0.count)) }
        XCTAssertGreaterThan(first, 0)
        XCTAssertEqual(bytes[Int(first)], 0)
        XCTAssertEqual(bytes[Int(first) + 1], 42)
        XCTAssertTrue(String(cString: bytes).contains("Communications"))
        XCTAssertEqual(bytes.withUnsafeMutableBufferPointer { mailbox.copy(into: $0.baseAddress, capacity: Int32($0.count)) }, 0)
        let generation = state.begin(status: "Connecting")
        var tiny = [CChar](repeating: 42, count: 2)
        XCTAssertEqual(tiny.withUnsafeMutableBufferPointer { mailbox.copy(into: $0.baseAddress, capacity: 2) }, 0)
        XCTAssertEqual(tiny, [42, 42])
        XCTAssertGreaterThan(bytes.withUnsafeMutableBufferPointer { mailbox.copy(into: $0.baseAddress, capacity: Int32($0.count)) }, 0)
        XCTAssertTrue(String(cString: bytes).contains("Connecting"))
        _ = state.clear(generation: generation, status: "Disconnected")
        XCTAssertGreaterThan(bytes.withUnsafeMutableBufferPointer { mailbox.copy(into: $0.baseAddress, capacity: Int32($0.count)) }, 0)
        XCTAssertTrue(String(cString: bytes).contains("Disconnected"))
        XCTAssertTrue(String(cString: bytes).contains("No live result"))
    }
}
