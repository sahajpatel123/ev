// swift-tools-version:5.10
import PackageDescription

let package = Package(
    name: "evhands",
    platforms: [.macOS(.v14)],
    products: [.executable(name: "evhands", targets: ["evhands"])],
    targets: [.executableTarget(name: "evhands", path: "Sources/evhands")]
)
