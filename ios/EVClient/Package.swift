// swift-tools-version: 5.10

import PackageDescription

let package = Package(
    name: "EVClient",
    platforms: [
        .iOS(.v17),
        .watchOS(.v10),
        .macOS(.v14),
    ],
    products: [
        .library(name: "EVClient", targets: ["EVClient"]),
        .executable(name: "EVClientCheck", targets: ["EVClientCheck"]),
    ],
    targets: [
        .target(name: "EVClient"),
        .executableTarget(name: "EVClientCheck", dependencies: ["EVClient"]),
    ]
)
