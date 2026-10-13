import XCTest
import NIOCore
import NIOEmbedded
import NIOSSH
@testable import CommsTransport

final class TrustTests: XCTestCase {
    let board = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIC7by9bvClMnRjk3KKoR+QpRdhuUXhIhVPC1+F2FnV39"
    let other = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAILfypXFWxIhmHZ1nZZKsNKIYRZvnXrra4sqWpnBRy66i"

    func testRevocationPreventsDeferredAuthenticationFromRecoveringEitherPassword() {
        let credentials = CredentialVault(board: "isolated-board-fixture", jump: "isolated-jump-fixture")
        let deferred = credentials
        credentials.clear()
        XCTAssertTrue(deferred.password(jumpHop: false) == nil)
        XCTAssertTrue(deferred.password(jumpHop: true) == nil)
    }

    func testAuthDelegateCannotOfferPasswordAfterVaultRevocation() throws {
        let loop = EmbeddedEventLoop()
        let credentials = CredentialVault(board: "isolated-board-fixture", jump: nil)
        let authentication = PasswordAuthentication(username: "fixture", credentials: credentials, jumpHop: false)
        credentials.clear()
        assertAuthenticationFailure(authentication, methods: .password, loop: loop, status: "Board credentials are unavailable")
    }

    func testPasswordOffersPreserveEachHopsCredential() throws {
        let loop = EmbeddedEventLoop()
        let credentials = CredentialVault(board: "Board-Fixture-123!", jump: "Jump-Fixture-456!")
        for jumpHop in [false, true] {
            let authentication = PasswordAuthentication(username: "fixture", credentials: credentials, jumpHop: jumpHop)
            let offer = try XCTUnwrap(authenticate(authentication, methods: .all, loop: loop).wait())
            XCTAssertEqual(offer.username, "fixture")
            guard case .password(let password) = offer.offer else { XCTFail("Expected password offer"); continue }
            XCTAssertEqual(password.password, jumpHop ? "Jump-Fixture-456!" : "Board-Fixture-123!")
        }
    }

    func testServerWithoutPasswordMethodIsDistinguishedAfterInitialOffer() throws {
        let loop = EmbeddedEventLoop()
        for jumpHop in [false, true] {
            let credentials = CredentialVault(board: "board-fixture", jump: "jump-fixture")
            let authentication = PasswordAuthentication(username: "fixture", credentials: credentials, jumpHop: jumpHop)
            // NIOSSH starts with .all before learning the server's actual methods.
            XCTAssertNotNil(try authenticate(authentication, methods: .all, loop: loop).wait())
            assertAuthenticationFailure(authentication, methods: .publicKey, loop: loop,
                                        status: "\(jumpHop ? "Jump host" : "Board") does not offer password authentication")
        }
    }

    func testPasswordMethodMustBeAvailableBeforeAnyOffer() {
        let loop = EmbeddedEventLoop()
        let credentials = CredentialVault(board: "board-fixture", jump: "jump-fixture")
        let authentication = PasswordAuthentication(username: "fixture", credentials: credentials, jumpHop: true)
        assertAuthenticationFailure(authentication, methods: [], loop: loop, status: "Jump host does not offer password authentication")
    }

    func testServerRejectionIdentifiesHopWithoutOfferingPasswordAgain() throws {
        let loop = EmbeddedEventLoop()
        for jumpHop in [false, true] {
            let credentials = CredentialVault(board: "board-fixture", jump: "jump-fixture")
            let authentication = PasswordAuthentication(username: "fixture", credentials: credentials, jumpHop: jumpHop)
            XCTAssertNotNil(try authenticate(authentication, methods: .all, loop: loop).wait())
            for _ in 0..<2 {
                assertAuthenticationFailure(authentication, methods: .password, loop: loop,
                                            status: "\(jumpHop ? "Jump host" : "Board") did not accept password authentication")
            }
        }
    }

    func testEmptyAndUnavailableCredentialsAreDistinguishedForEachHop() {
        let loop = EmbeddedEventLoop()
        for jumpHop in [false, true] {
            let hop = jumpHop ? "Jump host" : "Board"
            let empty = CredentialVault(board: "", jump: "")
            assertAuthenticationFailure(PasswordAuthentication(username: "fixture", credentials: empty, jumpHop: jumpHop),
                                        methods: .password, loop: loop, status: "\(hop) password is empty")
            let revoked = CredentialVault(board: "board-fixture", jump: "jump-fixture")
            revoked.clear()
            assertAuthenticationFailure(PasswordAuthentication(username: "fixture", credentials: revoked, jumpHop: jumpHop),
                                        methods: .password, loop: loop, status: "\(hop) credentials are unavailable")
        }
        let missing = CredentialVault(board: "board-fixture", jump: nil)
        assertAuthenticationFailure(PasswordAuthentication(username: "fixture", credentials: missing, jumpHop: true),
                                    methods: .password, loop: loop, status: "Jump host credentials are unavailable")
    }

    private func authenticate(_ authentication: PasswordAuthentication, methods: NIOSSHAvailableUserAuthenticationMethods,
                              loop: EmbeddedEventLoop) -> EventLoopFuture<NIOSSHUserAuthenticationOffer?> {
        let offer = loop.makePromise(of: NIOSSHUserAuthenticationOffer?.self)
        authentication.nextAuthenticationType(availableMethods: methods, nextChallengePromise: offer)
        return offer.futureResult
    }

    private func assertAuthenticationFailure(_ authentication: PasswordAuthentication, methods: NIOSSHAvailableUserAuthenticationMethods,
                                             loop: EmbeddedEventLoop, status: String, file: StaticString = #filePath, line: UInt = #line) {
        XCTAssertThrowsError(try authenticate(authentication, methods: methods, loop: loop).wait(), file: file, line: line) { error in
            guard let failure = error as? TransportFailure, case .password = failure else {
                XCTFail("Expected terminal password failure", file: file, line: line); return
            }
            XCTAssertEqual(failure.status, status, file: file, line: line)
        }
    }

    // A server with a valid but unenrolled key must not pass authentication.
    func testExactHostKeyRejectsDifferentValidKey() throws {
        let loop = EmbeddedEventLoop()
        let check = try PinnedHostKey(board)
        let accepted = loop.makePromise(of: Void.self)
        check.validateHostKey(hostKey: try NIOSSHPublicKey(openSSHPublicKey: board), validationCompletePromise: accepted)
        XCTAssertNoThrow(try accepted.futureResult.wait())
        let rejected = loop.makePromise(of: Void.self)
        check.validateHostKey(hostKey: try NIOSSHPublicKey(openSSHPublicKey: other), validationCompletePromise: rejected)
        XCTAssertThrowsError(try rejected.futureResult.wait())
    }
}
