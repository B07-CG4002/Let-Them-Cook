#if os(macOS)
import Darwin
import Foundation
import NIOCore
import XCTest
import CommsCore
@testable import CommsTransport

/// Uses the actual Python CLI and ingestion contract, with two real loopback SSH
/// hops. This is native/server interoperability, not a physical BLE/device test.
final class PythonBoardTests: XCTestCase {
    func testActualPythonBoardDelivers100UniqueResultsThroughBothSSHHops() throws {
        let repository = (0..<5).reduce(URL(fileURLWithPath: #filePath)) { path, _ in path.deletingLastPathComponent() }
        let pki = try TestPKI()
        let listening = expectation(description: "Python board listening")
        let ports = LockedPorts()
        let board = PythonTestProcess(arguments: [
            "-u", "-m", "ultra96.server",
            "--cert", pki.directory.appendingPathComponent("server.pem").path,
            "--key", pki.directory.appendingPathComponent("server.key").path,
            "--ingest-port", "0", "--gateway-port", "0",
        ], repository: repository) { line in
            guard let event = Self.object(line), event["event"] as? String == "listening",
                  let ingest = event["ingest_port"] as? Int, let gateway = event["gateway_port"] as? Int else { return }
            ports.set(ingest: ingest, gateway: gateway)
            listening.fulfill()
        }
        try board.start()
        defer { board.stop() }
        wait(for: [listening], timeout: 10)
        let (ingestPort, gatewayPort) = try XCTUnwrap(ports.get(), "Python startup: \(board.errors)")

        let peers = try LocalPeers(pki: pki, externalServicePort: gatewayPort)
        var options = peers.options
        options.connectTimeout = .seconds(10)
        options.frameTimeout = .seconds(5)
        let subscribed = expectation(description: "native subscriber acknowledged by Python")
        subscribed.assertForOverFulfill = true
        let received = expectation(description: "100 native validated results")
        received.expectedFulfillmentCount = 100
        received.assertForOverFulfill = true
        let results = LockedResults()
        let client = CommsClient(route: peers.route(), session: "ltc-comms", options: options, onStatus: { status in
            if status == "Subscribed" { subscribed.fulfill() }
            else if status.hasPrefix("Disconnected") { XCTFail("Python/native session failed: \(status)") }
        }, onResult: { result in
            results.append(result)
            received.fulfill()
        })
        defer { client.stop() }
        client.start()
        guard XCTWaiter.wait(for: [subscribed], timeout: 10) == .completed else {
            XCTFail("Native SUBSCRIBED was not received; ingestion was not started")
            return
        }

        // Do not ingest before SUBSCRIBED: the board deliberately has no replay.
        let ingest = PythonTestProcess(arguments: ["-u", "-c", Self.ingestProgram,
            pki.directory.appendingPathComponent("ca.pem").path, String(ingestPort)], repository: repository)
        try ingest.start()
        defer { ingest.stop() }
        wait(for: [received], timeout: 15)
        XCTAssertTrue(ingest.waitForExit(timeout: 5), "Python ingestion did not exit")
        XCTAssertEqual(ingest.exitStatus, 0, "Python ingestion: \(ingest.errors)")
        XCTAssertEqual(ingest.lines.compactMap(Self.object).first?["accepted"] as? Int, 100)
        client.stop()

        let observed = results.get()
        XCTAssertEqual(observed.count, 100)
        XCTAssertEqual(observed.map(\.seq), (0..<100).map(UInt32.init))
        XCTAssertEqual(observed.map(\.resultID), (0..<100).map { "1:7:\($0)" })
        XCTAssertEqual(Set(observed.map(\.resultID)).count, 100)
        XCTAssertEqual(observed.first { $0.seq == 42 }?.gesture, "OPEN")

        board.terminate()
        XCTAssertTrue(board.waitForExit(timeout: 5), "Python board did not shut down")
        XCTAssertEqual(board.exitStatus, 0, "Python shutdown: \(board.errors)")
        let stopped = try XCTUnwrap(board.lines.compactMap(Self.object).first { $0["event"] as? String == "stopped" })
        let metrics = try XCTUnwrap(stopped["metrics"] as? [String: Any])
        XCTAssertEqual(metrics["accepted"] as? Int, 100)
        XCTAssertEqual(metrics["subscribers"] as? Int, 1)
        XCTAssertEqual(metrics["duplicates"] as? Int, 0)
        XCTAssertEqual(metrics["rejected"] as? Int, 0)
        XCTAssertEqual(metrics["disconnected_results"] as? Int, 0)
        XCTAssertEqual(metrics["result_drops"] as? Int, 0)
        XCTAssertEqual(metrics["result_stale"] as? Int, 0)
        withExtendedLifetime(peers) {}
    }

    private static func object(_ line: String) -> [String: Any]? {
        (try? JSONSerialization.jsonObject(with: Data(line.utf8))) as? [String: Any]
    }

    private static let ingestProgram = #"""
    import asyncio, json, sys
    from common.sensor import dummy_values
    from common.tls import TLS_SERVER_NAME, client_context
    from common.wire import read_frame, write_frame

    async def run():
        reader, writer = await asyncio.wait_for(asyncio.open_connection(
            "127.0.0.1", int(sys.argv[2]), ssl=client_context(sys.argv[1]),
            server_hostname=TLS_SERVER_NAME, ssl_handshake_timeout=5.0), 5.0)
        try:
            for seq in range(100):
                await write_frame(writer, dict(v=1, type="SENSOR_BATCH", session_id="ltc-comms",
                    device_id=1, boot_id=7, seq=seq, uptime_ms=seq * 100,
                    values=list(dummy_values(seq))))
                ack = await read_frame(reader)
                assert ack == dict(v=1, type="INGEST_ACK", session_id="ltc-comms",
                    device_id=1, boot_id=7, seq=seq, status="accepted"), "unexpected ingestion ACK"
                await asyncio.sleep(0.02)
        finally:
            writer.close()
            await asyncio.wait_for(writer.wait_closed(), 2.0)
        print(json.dumps(dict(accepted=100)), flush=True)

    asyncio.run(run())
    """#
}

private final class LockedPorts {
    private let lock = NSLock()
    private var ports: (Int, Int)?
    func set(ingest: Int, gateway: Int) { lock.lock(); ports = (ingest, gateway); lock.unlock() }
    func get() -> (Int, Int)? { lock.lock(); defer { lock.unlock() }; return ports }
}

private final class LockedResults {
    private let lock = NSLock()
    private var results: [GestureResult] = []
    func append(_ result: GestureResult) { lock.lock(); results.append(result); lock.unlock() }
    func get() -> [GestureResult] { lock.lock(); defer { lock.unlock() }; return results }
}

/// Every process has bounded waits and a kill fallback. Output capture is capped
/// and contains public counters only; temporary PEM/private-key bytes are unread.
private final class PythonTestProcess {
    private let process = Process()
    private let output = Pipe()
    private let error = Pipe()
    private let finished = DispatchSemaphore(value: 0)
    private let drains = DispatchGroup()
    private let stdout: ProcessLines
    private let stderr = ProcessLines()

    init(arguments: [String], repository: URL, onLine: @escaping (String) -> Void = { _ in }) {
        stdout = ProcessLines(onLine: onLine)
        process.executableURL = URL(fileURLWithPath: "/usr/bin/env")
        process.arguments = ["python3"] + arguments
        process.currentDirectoryURL = repository
        process.standardOutput = output
        process.standardError = error
        process.standardInput = FileHandle.nullDevice
        let finished = self.finished
        process.terminationHandler = { _ in finished.signal() }
    }

    var lines: [String] { stdout.lines }
    var errors: String { stderr.lines.joined(separator: "\n") }
    var exitStatus: Int32? { process.isRunning ? nil : process.terminationStatus }

    func start() throws {
        try process.run()
        drain(output.fileHandleForReading, into: stdout)
        drain(error.fileHandleForReading, into: stderr)
    }

    private func drain(_ handle: FileHandle, into collector: ProcessLines) {
        drains.enter()
        let drains = self.drains
        DispatchQueue.global(qos: .utility).async {
            defer { drains.leave() }
            var pending = Data()
            while true {
                // read(upToCount:) waits for the requested count or EOF on this
                // Foundation runtime; availableData delivers the readiness line.
                let bytes = handle.availableData
                if bytes.isEmpty { break }
                pending.append(bytes)
                while let newline = pending.firstIndex(of: 10) {
                    collector.append(String(decoding: pending[..<newline], as: UTF8.self))
                    pending.removeSubrange(...newline)
                }
                if pending.count > 16_384 { pending = Data(pending.suffix(16_384)) }
            }
            if !pending.isEmpty { collector.append(String(decoding: pending, as: UTF8.self)) }
        }
    }

    func waitForExit(timeout: TimeInterval) -> Bool {
        let deadline = DispatchTime.now() + timeout
        if process.isRunning, finished.wait(timeout: deadline) == .timedOut { return false }
        return drains.wait(timeout: deadline) == .success
    }

    func terminate() { if process.isRunning { process.terminate() } }

    func stop() {
        guard process.processIdentifier != 0 else { return }
        terminate()
        if !waitForExit(timeout: 3), process.isRunning {
            Darwin.kill(process.processIdentifier, SIGKILL)
            _ = waitForExit(timeout: 3)
        }
    }
}

private final class ProcessLines {
    private let lock = NSLock()
    private var captured: [String] = []
    private var byteCount = 0
    private let onLine: (String) -> Void
    init(onLine: @escaping (String) -> Void = { _ in }) { self.onLine = onLine }
    var lines: [String] { lock.lock(); defer { lock.unlock() }; return captured }
    func append(_ line: String) {
        lock.lock()
        if byteCount < 16_384 {
            let bounded = String(line.prefix(4096))
            captured.append(bounded)
            byteCount += bounded.utf8.count
        }
        lock.unlock()
        onLine(line)
    }
}
#endif
