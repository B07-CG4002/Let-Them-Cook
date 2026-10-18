import Foundation

/// Pending DNS/channel callbacks hold this revocable reference, never a copied route password.
final class CredentialVault {
    private let lock = NSLock()
    private var board: String?
    private var jump: String?
    init(board: String, jump: String?) { self.board = board; self.jump = jump }
    func password(jumpHop: Bool) -> String? {
        lock.lock(); defer { lock.unlock() }; return jumpHop ? jump : board
    }
    func clear() { lock.lock(); board = nil; jump = nil; lock.unlock() }
}

/// The data captured by asynchronous connection attempts excludes all secrets.
struct RouteEndpoints {
    struct Jump { let host: String; let user: String; let hostKey: String }
    let boardHost: String
    let boardUser: String
    let boardHostKey: String
    let jump: Jump?
    let caPEM: String
    init(_ route: SSHRoute) {
        boardHost = route.boardHost; boardUser = route.boardUser; boardHostKey = route.boardHostKey
        jump = route.jump.map { .init(host: $0.host, user: $0.user, hostKey: $0.hostKey) }
        caPEM = route.caPEM
    }
}
