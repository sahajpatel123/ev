// swift-tools-version: 5.10

import PackageDescription

let package = Package(
    name: "EV",
    platforms: [
        .macOS(.v14),
    ],
    products: [
        .library(name: "EVAuth", targets: ["EVAuth"]),
        .library(name: "EVRuntime", targets: ["EVRuntime"]),
        .executable(name: "EV", targets: ["EV"]),
        .executable(name: "EVNotificationHelper", targets: ["EVNotificationHelper"]),
        .executable(name: "EVLifeHelper", targets: ["EVLifeHelper"]),
        .executable(name: "EVAuthCheck", targets: ["EVAuthCheck"]),
        .executable(name: "EVMicTalkTests", targets: ["EVMicTalkTests"]),
    ],
    dependencies: [
        .package(path: "../ios/EVClient"),
    ],
    targets: [
        .target(
            name: "EVAuth",
            path: "Sources/EVAuth"
        ),
        .target(
            name: "EVRuntimeObjC",
            path: "Sources/EVObjCSupport",
            publicHeadersPath: "include",
            linkerSettings: [
                .linkedFramework("AVFAudio"),
                .linkedFramework("UserNotifications"),
            ]
        ),
        .target(
            name: "EVRuntime",
            dependencies: ["EVRuntimeObjC"],
            path: "Sources/EVRuntime"
        ),
        .executableTarget(
            name: "EV",
            dependencies: [
                "EVAuth",
                "EVRuntime",
                .product(name: "EVClient", package: "EVClient"),
                .product(name: "EVUI", package: "EVClient"),
            ],
            path: "Sources/EV"
        ),
        .executableTarget(
            name: "EVMicTalkTests",
            dependencies: ["EVRuntime"],
            path: "Tests/EVTests"
        ),
        .executableTarget(
            name: "EVAuthCheck",
            dependencies: ["EVAuth"],
            path: "Sources/EVAuthCheck"
        ),
        .executableTarget(
            name: "EVNotificationHelper",
            dependencies: [],
            path: "Sources/EVNotificationHelper"
        ),
        .executableTarget(
            name: "EVLifeHelper",
            dependencies: [],
            path: "Sources/EVLifeHelper"
        ),
    ]
)
