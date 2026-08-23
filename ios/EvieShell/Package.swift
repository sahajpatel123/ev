// swift-tools-version: 5.10

import PackageDescription

let package = Package(
    name: "EvieNativeBroker",
    platforms: [
        .iOS(.v17),
        .macOS(.v14),
    ],
    products: [
        .library(name: "EvieNativeBroker", targets: ["EvieNativeBroker"]),
        .executable(name: "EvieBrokerCheck", targets: ["EvieBrokerCheck"]),
    ],
    targets: [
        .target(name: "EvieNativeBroker"),
        .executableTarget(name: "EvieBrokerCheck", dependencies: ["EvieNativeBroker"]),
    ]
)
