import Foundation
import NIOCore
import NIOPosix

/// Public connection configuration. Passwords are memory-only; this type is deliberately not Codable.
public struct SSHRoute {
    public struct Jump {
        public let host: String
        public let user: String
        public let password: String
        public let hostKey: String
        public init(host: String, user: String, password: String, hostKey: String) {
            self.host = host; self.user = user; self.password = password; self.hostKey = hostKey
        }
    }
    public let boardHost: String
    public let boardUser: String
    public let boardPassword: String
    public let boardHostKey: String
    public let jump: Jump?
    public let caPEM: String
    public init(boardHost: String, boardUser: String, boardPassword: String, boardHostKey: String, jump: Jump? = nil, caPEM: String) {
        self.boardHost = boardHost; self.boardUser = boardUser; self.boardPassword = boardPassword
        self.boardHostKey = boardHostKey; self.jump = jump; self.caPEM = caPEM
    }
}

// Internal injection is confined to local fixtures. The app-facing initializer uses fixed ports.
struct TransportOptions {
    var resolver: (Resolver & Sendable)?
    var boardPort = 22
    var jumpPort = 22
    var servicePort = 9999
    var connectTimeout = TimeAmount.seconds(10)
    var frameTimeout = TimeAmount.seconds(5)
    var retryMinimum = TimeAmount.milliseconds(500)
    var retryMaximum = TimeAmount.seconds(5)
}

enum SSHAuthenticationHop: String {
    case board = "Board"
    case jump = "Jump host"
}

enum PasswordFailureReason {
    case methodUnavailable, rejected, credentialsUnavailable, empty
    var status: String {
        switch self {
        case .methodUnavailable: return "does not offer password authentication"
        case .rejected: return "did not accept password authentication"
        case .credentialsUnavailable: return "credentials are unavailable"
        case .empty: return "password is empty"
        }
    }
}

enum TransportFailure: Error {
    case hostKey, invalidData, invalidConfiguration, deadline, frameDeadline, disconnected
    case password(hop: SSHAuthenticationHop, reason: PasswordFailureReason)
    var status: String {
        switch self {
        case .hostKey: return "SSH host key rejected"
        case .password(let hop, let reason): return "\(hop.rawValue) \(reason.status)"
        case .invalidData: return "invalid SSH stream data"
        case .invalidConfiguration: return "invalid connection configuration"
        case .deadline: return "connection deadline exceeded"
        case .frameDeadline: return "frame deadline exceeded"
        case .disconnected: return "connection closed"
        }
    }
}
