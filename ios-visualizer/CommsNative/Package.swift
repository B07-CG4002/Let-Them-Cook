// swift-tools-version: 5.10
import PackageDescription

let package = Package(
    name: "CommsNative",
    platforms: [.iOS(.v15), .macOS(.v13)],
    products: [.library(name: "CommsNative", type: .static, targets: ["CommsBridge"])],
    dependencies: [
        .package(url: "https://github.com/apple/swift-nio.git", exact: "2.102.0"),
        .package(url: "https://github.com/apple/swift-nio-ssh.git", exact: "0.15.0"),
        .package(url: "https://github.com/apple/swift-nio-ssl.git", exact: "2.37.4"),
        .package(url: "https://github.com/apple/swift-crypto.git", exact: "4.5.2"),
    ],
    targets: [
        .target(name: "CommsCore"),
        .target(name: "CommsTransport", dependencies: ["CommsCore",
            .product(name: "NIOCore", package: "swift-nio"),
            .product(name: "NIOPosix", package: "swift-nio"),
            .product(name: "NIOTLS", package: "swift-nio"),
            .product(name: "NIOSSH", package: "swift-nio-ssh"),
            .product(name: "NIOSSL", package: "swift-nio-ssl"),
            .product(name: "Crypto", package: "swift-crypto")]),
        .target(name: "CommsBridge", dependencies: ["CommsCore", "CommsTransport"]),
        .testTarget(name: "CommsCoreTests", dependencies: ["CommsCore"]),
        .testTarget(name: "CommsBridgeTests", dependencies: ["CommsBridge"]),
        .testTarget(name: "CommsTransportTests", dependencies: ["CommsTransport", "CommsCore",
            .product(name: "NIOEmbedded", package: "swift-nio"),
            .product(name: "NIOSSH", package: "swift-nio-ssh"),
            .product(name: "NIOSSL", package: "swift-nio-ssl"),
            .product(name: "Crypto", package: "swift-crypto")]),
    ]
)
