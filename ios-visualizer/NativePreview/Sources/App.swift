import UIKit
import CommsBridge

// Simulator harness for the SAME native module linked by UnityFramework.
// It intentionally provides no fake gestures or alternative network client.
@main final class AppDelegate: UIResponder, UIApplicationDelegate {}

final class SceneDelegate: UIResponder, UIWindowSceneDelegate {
    var window: UIWindow?
    func scene(_ scene: UIScene, willConnectTo session: UISceneSession, options: UIScene.ConnectionOptions) {
        guard let scene = scene as? UIWindowScene else { return }
        let window = UIWindow(windowScene: scene)
        window.rootViewController = PreviewController()
        self.window = window
        window.makeKeyAndVisible()
    }
}

final class PreviewController: UIViewController {
    private let text = UITextView()
    private var timer: Timer?
    override func viewDidLoad() {
        super.viewDidLoad(); view.backgroundColor = .systemBackground
        text.isEditable = false; text.font = .monospacedSystemFont(ofSize: 22, weight: .regular)
        text.accessibilityIdentifier = "comms.display"
        text.translatesAutoresizingMaskIntoConstraints = false; view.addSubview(text)
        NSLayoutConstraint.activate([
            text.leadingAnchor.constraint(equalTo: view.safeAreaLayoutGuide.leadingAnchor, constant: 24),
            text.trailingAnchor.constraint(equalTo: view.safeAreaLayoutGuide.trailingAnchor, constant: -24),
            text.topAnchor.constraint(equalTo: view.safeAreaLayoutGuide.topAnchor, constant: 72),
            text.bottomAnchor.constraint(equalTo: view.safeAreaLayoutGuide.bottomAnchor, constant: -16)
        ])
    }
    override func viewDidAppear(_ animated: Bool) {
        super.viewDidAppear(animated)
        guard timer == nil else { return }
        commsStart()
        timer = Timer.scheduledTimer(withTimeInterval: 0.05, repeats: true) { [weak self] _ in
            var buffer = [CChar](repeating: 0, count: 2048)
            let count = buffer.withUnsafeMutableBufferPointer { commsCopyDisplay($0.baseAddress, Int32($0.count)) }
            if count > 0 { self?.text.text = String(cString: buffer) }
        }
    }
    deinit { timer?.invalidate(); commsStop() }
}
