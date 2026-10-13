#if os(iOS)
import UIKit
import CommsCore
import CommsTransport

/// Owns the native foreground session. Unity retains no Swift/async pointers.
final class IntegrationController {
    static let shared = IntegrationController()
    private var state: DisplayState?
    private var client: CommsClient?
    private var button: UIButton?
    private weak var form: SetupViewController?
    private var observers: [NSObjectProtocol] = []
    private let store: ConfigurationStore
    private init() {
        let support = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
        store = ConfigurationStore(directory: support.appendingPathComponent("Comms", isDirectory: true),
                                   legacyDirectory: support.appendingPathComponent("Week7", isDirectory: true))
    }

    func install(state: DisplayState) {
        dispatchPrecondition(condition: .onQueue(.main))
        guard self.state == nil else { return }
        self.state = state
        _ = state.stop(status: "Tap Communications Connect to configure this iPhone")
        let center = NotificationCenter.default
        observers.append(center.addObserver(forName: UIApplication.willResignActiveNotification, object: nil, queue: .main) { [weak self] _ in
            self?.disconnect(status: "Paused — return and connect again")
        })
        observers.append(center.addObserver(forName: UIApplication.didBecomeActiveNotification, object: nil, queue: .main) { [weak self] _ in
            self?.attachButton()
        })
        attachButton()
    }

    private func rootController() -> UIViewController? {
        UIApplication.shared.connectedScenes.compactMap { $0 as? UIWindowScene }
            .filter { $0.activationState == .foregroundActive }
            .flatMap(\.windows).first(where: \.isKeyWindow)?.rootViewController
    }

    private func attachButton() {
        guard let root = rootController(), button == nil else { return }
        let button = UIButton(type: .system)
        var style = UIButton.Configuration.filled()
        style.title = "Communications Connect"
        style.baseBackgroundColor = .systemIndigo
        style.cornerStyle = .capsule
        button.configuration = style
        button.accessibilityIdentifier = "comms.setup"
        button.addAction(UIAction { [weak self] _ in self?.showSetup() }, for: .touchUpInside)
        button.translatesAutoresizingMaskIntoConstraints = false
        root.view.addSubview(button)
        NSLayoutConstraint.activate([
            button.topAnchor.constraint(equalTo: root.view.safeAreaLayoutGuide.topAnchor, constant: 10),
            button.trailingAnchor.constraint(equalTo: root.view.safeAreaLayoutGuide.trailingAnchor, constant: -12)
        ])
        self.button = button
    }

    private func showSetup() {
        guard let root = rootController(), form == nil else { return }
        let setup = SetupViewController(configuration: store.load(), caPEM: store.loadCA(), connected: client != nil)
        setup.onConnect = { [weak self] config, ca, boardPassword, jumpPassword in
            guard let self = self, let state = self.state else { return }
            try self.store.save(config, caPEM: ca)
            self.disconnect(status: "Connecting")
            let generation = state.begin(status: "Connecting")
            let jump = config.useJump ? SSHRoute.Jump(host: EnrolledTrust.jumpHost, user: config.jumpUser,
                                                       password: jumpPassword, hostKey: EnrolledTrust.jumpKey) : nil
            let route = SSHRoute(boardHost: EnrolledTrust.boardHost, boardUser: config.boardUser,
                                 boardPassword: boardPassword, boardHostKey: EnrolledTrust.boardKey,
                                 jump: jump, caPEM: ca)
            let client = CommsClient(route: route, session: "ltc-comms", onStatus: { status in
                if status == "Subscribed" { _ = state.setStatus(status, generation: generation) }
                else { _ = state.clear(generation: generation, status: status) }
            }, onResult: { result in
                _ = state.accept(result, generation: generation)
            })
            self.client = client
            self.button?.configuration?.title = "Communications Settings"
            client.start()
        }
        setup.onDisconnect = { [weak self] in self?.disconnect(status: "Disconnected") }
        form = setup
        let navigation = UINavigationController(rootViewController: setup)
        navigation.modalPresentationStyle = .formSheet
        root.present(navigation, animated: true)
    }

    private func disconnect(status: String) {
        // Clear ownership before stopping: even synchronous late callbacks fail
        // the display generation check. Releasing the client forgets its route.
        _ = state?.stop(status: status)
        let old = client; client = nil
        old?.stop()
        form?.clearPasswords()
        button?.configuration?.title = "Communications Connect"
    }

    func uninstall() {
        disconnect(status: "Stopped")
        observers.forEach(NotificationCenter.default.removeObserver)
        observers.removeAll()
        form?.dismiss(animated: false); form = nil
        button?.removeFromSuperview(); button = nil
        state = nil
    }
}
#endif
