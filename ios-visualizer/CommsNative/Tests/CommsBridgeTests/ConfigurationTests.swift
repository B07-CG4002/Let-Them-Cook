import XCTest
@testable import CommsBridge

final class ConfigurationTests: XCTestCase {
    func testConfigurationNeverSerializesPasswordFields() throws {
        let config = PublicConfiguration(boardUser: "board-user", jumpUser: "jump-user", useJump: true)
        let data = try JSONEncoder().encode(config)
        let fields = try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
        XCTAssertEqual(Set(fields.keys), ["boardUser", "jumpUser", "useJump"])
        XCTAssertEqual(try JSONDecoder().decode(PublicConfiguration.self, from: data), config)
    }

    func testRejectsPrivateKeysAndUnrelatedCertificate() throws {
        XCTAssertThrowsError(try AuthorityImport.validate("-----BEGIN PRIVATE KEY-----\nx\n-----END PRIVATE KEY-----"))
        let root = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
        let unrelated = try String(contentsOf: root.appendingPathComponent("reference/teammate-localhost-cert.pem"))
        XCTAssertThrowsError(try AuthorityImport.validate(unrelated)) { error in
            guard case SetupError.wrongAuthority = error else { return XCTFail("Expected enrolled-CA mismatch") }
        }
        XCTAssertThrowsError(try AuthorityImport.validate("not a certificate"))
        XCTAssertThrowsError(try AuthorityImport.validate(String(repeating: "a", count: 65_537)))
    }

    func testRejectsMissingUsernamesAndControlCharacters() throws {
        XCTAssertThrowsError(try PublicConfiguration(boardUser: "", jumpUser: "u", useJump: true).validate())
        XCTAssertThrowsError(try PublicConfiguration(boardUser: "u\n", jumpUser: "u", useJump: true).validate())
        XCTAssertThrowsError(try PublicConfiguration(boardUser: "u", jumpUser: "", useJump: true).validate())
        XCTAssertNoThrow(try PublicConfiguration(boardUser: "u", jumpUser: "", useJump: false).validate())
    }

    func testLoadsLegacySettingsUntilCurrentSettingsExist() throws {
        let support = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString, isDirectory: true)
        defer { try? FileManager.default.removeItem(at: support) }
        let current = support.appendingPathComponent("Comms", isDirectory: true)
        let legacy = support.appendingPathComponent("Week7", isDirectory: true)
        try FileManager.default.createDirectory(at: legacy, withIntermediateDirectories: true)
        let legacySettings = PublicConfiguration(boardUser: "old-board", jumpUser: "old-jump", useJump: true)
        let legacyURL = legacy.appendingPathComponent("public-settings.json")
        try JSONEncoder().encode(legacySettings).write(to: legacyURL)

        let store = ConfigurationStore(directory: current, legacyDirectory: legacy)
        XCTAssertEqual(store.load(), legacySettings)
        XCTAssertFalse(FileManager.default.fileExists(atPath: current.path))

        try FileManager.default.createDirectory(at: current, withIntermediateDirectories: true)
        let currentSettings = PublicConfiguration(boardUser: "new-board", jumpUser: "", useJump: false)
        try JSONEncoder().encode(currentSettings).write(to: current.appendingPathComponent("public-settings.json"))
        XCTAssertEqual(store.load(), currentSettings)
        let preservedLegacy = try JSONDecoder().decode(PublicConfiguration.self, from: Data(contentsOf: legacyURL))
        XCTAssertEqual(preservedLegacy, legacySettings)
    }

    func testInvalidCurrentSettingsDoNotRestoreLegacyValues() throws {
        let support = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString, isDirectory: true)
        defer { try? FileManager.default.removeItem(at: support) }
        let current = support.appendingPathComponent("Comms", isDirectory: true)
        let legacy = support.appendingPathComponent("Week7", isDirectory: true)
        try FileManager.default.createDirectory(at: current, withIntermediateDirectories: true)
        try FileManager.default.createDirectory(at: legacy, withIntermediateDirectories: true)
        let legacySettings = PublicConfiguration(boardUser: "old-board", jumpUser: "old-jump", useJump: true)
        try JSONEncoder().encode(legacySettings).write(to: legacy.appendingPathComponent("public-settings.json"))
        try Data("invalid JSON".utf8).write(to: current.appendingPathComponent("public-settings.json"))

        let store = ConfigurationStore(directory: current, legacyDirectory: legacy)
        XCTAssertEqual(store.load(), PublicConfiguration())
    }

    func testLegacyCAStillRequiresEnrolledAuthority() throws {
        let support = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString, isDirectory: true)
        defer { try? FileManager.default.removeItem(at: support) }
        let current = support.appendingPathComponent("Comms", isDirectory: true)
        let legacy = support.appendingPathComponent("Week7", isDirectory: true)
        try FileManager.default.createDirectory(at: legacy, withIntermediateDirectories: true)
        let root = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
        let unrelated = try Data(contentsOf: root.appendingPathComponent("reference/teammate-localhost-cert.pem"))
        let legacyCA = legacy.appendingPathComponent("week7-ca.pem")
        try unrelated.write(to: legacyCA)

        let store = ConfigurationStore(directory: current, legacyDirectory: legacy)
        XCTAssertNil(store.loadCA())
        XCTAssertFalse(FileManager.default.fileExists(atPath: current.path))
        XCTAssertEqual(try Data(contentsOf: legacyCA), unrelated)
    }
}
