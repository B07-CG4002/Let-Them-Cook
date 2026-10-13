import Foundation
import CryptoKit
import Security

// These are PUBLIC, previously enrolled trust values from the Phone runbooks.
// Rotation requires independent verification and an explicit source update.
enum EnrolledTrust {
    static let boardHost = "makerslab-fpga-35.ddns.comp.nus.edu.sg"
    static let jumpHost = "stujump.comp.nus.edu.sg"
    static let boardKey = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIC7by9bvClMnRjk3KKoR+QpRdhuUXhIhVPC1+F2FnV39"
    static let jumpKey = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAILfypXFWxIhmHZ1nZZKsNKIYRZvnXrra4sqWpnBRy66i"
    static let caSHA256 = "4dfba4905c171e68c3623dbc952154149076ed89004b85d475e858cc550760ec"
}

enum SetupError: Error, LocalizedError {
    case username, certificate, wrongAuthority
    var errorDescription: String? {
        switch self {
        case .username: return "Enter the required usernames without spaces or control characters."
        case .certificate: return "Import one public CA certificate in PEM format (maximum 64 KiB). Private keys are not accepted."
        case .wrongAuthority: return "This certificate does not match the enrolled Communications CA. Obtain the verified public ca-cert.pem from the Communications setup."
        }
    }
}

struct PublicConfiguration: Codable, Equatable {
    var boardUser = ""
    var jumpUser = ""
    var useJump = true

    func validate() throws {
        func valid(_ value: String) -> Bool {
            !value.isEmpty && value.utf8.count <= 128 &&
            value.unicodeScalars.allSatisfy { !CharacterSet.whitespacesAndNewlines.contains($0) && !CharacterSet.controlCharacters.contains($0) }
        }
        guard valid(boardUser), !useJump || valid(jumpUser) else { throw SetupError.username }
    }
}

enum AuthorityImport {
    static func validate(_ pem: String) throws -> String {
        guard pem.utf8.count <= 65_536,
              !pem.contains("PRIVATE KEY"),
              pem.components(separatedBy: "-----BEGIN CERTIFICATE-----").count == 2,
              pem.components(separatedBy: "-----END CERTIFICATE-----").count == 2 else { throw SetupError.certificate }
        let trimmed = pem.trimmingCharacters(in: .whitespacesAndNewlines)
        let begin = "-----BEGIN CERTIFICATE-----", end = "-----END CERTIFICATE-----"
        guard trimmed.hasPrefix(begin), trimmed.hasSuffix(end) else { throw SetupError.certificate }
        let encoded = trimmed.dropFirst(begin.count).dropLast(end.count).filter { !$0.isWhitespace }
        guard let der = Data(base64Encoded: String(encoded)),
              SecCertificateCreateWithData(nil, der as CFData) != nil else { throw SetupError.certificate }
        let fingerprint = SHA256.hash(data: der).map { String(format: "%02x", $0) }.joined()
        guard fingerprint == EnrolledTrust.caSHA256 else { throw SetupError.wrongAuthority }
        return trimmed + "\n"
    }
}

// Deliberately has no password fields. Secrets live only in UI/transport memory.
final class ConfigurationStore {
    private let directory: URL
    private let legacyDirectory: URL
    init(directory: URL, legacyDirectory: URL) {
        self.directory = directory
        self.legacyDirectory = legacyDirectory
    }
    private var configURL: URL { directory.appendingPathComponent("public-settings.json") }
    private var caURL: URL { directory.appendingPathComponent("comms-ca.pem") }
    private var legacyConfigURL: URL { legacyDirectory.appendingPathComponent("public-settings.json") }
    private var legacyCAURL: URL { legacyDirectory.appendingPathComponent("week7-ca.pem") }
    private func loadURL(current: URL, legacy: URL) -> URL {
        FileManager.default.fileExists(atPath: current.path) ? current : legacy
    }
    func load() -> PublicConfiguration {
        guard let data = try? Data(contentsOf: loadURL(current: configURL, legacy: legacyConfigURL)),
              let config = try? JSONDecoder().decode(PublicConfiguration.self, from: data) else { return PublicConfiguration() }
        return config
    }
    func loadCA() -> String? {
        guard let data = try? Data(contentsOf: loadURL(current: caURL, legacy: legacyCAURL)), data.count <= 65_536,
              let pem = String(data: data, encoding: .utf8) else { return nil }
        return try? AuthorityImport.validate(pem)
    }
    func save(_ configuration: PublicConfiguration, caPEM: String) throws {
        try configuration.validate()
        let validated = try AuthorityImport.validate(caPEM)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        var options: Data.WritingOptions = [.atomic]
        #if os(iOS)
        options.insert(.completeFileProtection)
        #endif
        try JSONEncoder().encode(configuration).write(to: configURL, options: options)
        try Data(validated.utf8).write(to: caURL, options: options)
        var excluded = directory
        var values = URLResourceValues(); values.isExcludedFromBackup = true
        try excluded.setResourceValues(values)
    }
}
