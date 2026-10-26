// swift-tools-version: 6.0

import PackageDescription

let package = Package(
    name: "EV",
    platforms: [
        .macOS(.v14),
    ],
    products: [
        .library(name: "EVAuth", targets: ["EVAuth"]),
        .library(name: "EVRuntime", targets: ["EVRuntime"]),
        .library(name: "PresenceGesture", targets: ["PresenceGesture"]),
        .library(name: "PresenceCore", targets: ["PresenceCore"]),
        .executable(name: "PresenceChecks", targets: ["PresenceChecks"]),
        .executable(name: "presence-check", targets: ["PresenceCheck"]),
        .executable(name: "EV", targets: ["EV"]),
        .executable(name: "EVNotificationHelper", targets: ["EVNotificationHelper"]),
        .executable(name: "EVLifeHelper", targets: ["EVLifeHelper"]),
        .executable(name: "EVAuthCheck", targets: ["EVAuthCheck"]),
        .executable(name: "EVMicTalkTests", targets: ["EVMicTalkTests"]),
    ],
    dependencies: [
        .package(path: "../ios/EVClient"),
        .package(url: "https://github.com/apple/swift-atomics.git", from: "1.2.0"),
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
            dependencies: [
                "EVRuntimeObjC",
            ],
            path: "Sources/EVRuntime"
        ),
        .executableTarget(
            name: "EV",
            dependencies: [
                "EVAuth",
                "EVRuntime",
                "PresenceGesture",
                "PresenceCore",
                .product(name: "EVClient", package: "EVClient"),
                .product(name: "EVUI", package: "EVClient"),
                .product(name: "Atomics", package: "swift-atomics"),
            ],
            path: "Sources/EV",
            linkerSettings: [
                .linkedFramework("Metal"),
                .linkedFramework("AVFoundation"),
                .linkedFramework("CoreVideo"),
                .linkedFramework("QuartzCore"),
                .linkedFramework("IOSurface"),
                .linkedFramework("ApplicationServices"),
                .linkedFramework("Carbon"),
                .linkedFramework("CoreGraphics"),
                .linkedFramework("Vision"),
                .linkedFramework("Photos"),
            ]
        ),
        .target(
            name: "PresenceGesture",
            path: "Sources/PresenceGesture"
        ),
        .target(
            name: "PresenceCore",
            path: "Sources/PresenceCore"
        ),
        .executableTarget(
            name: "PresenceChecks",
            dependencies: [
                "PresenceGesture",
                "PresenceCore",
            ],
            path: "PresenceChecks"
        ),
        .executableTarget(
            name: "PresenceCheck",
            dependencies: [
                "PresenceGesture",
                "PresenceCore",
            ],
            path: "PresenceCheck"
        ),
        .executableTarget(
            name: "EVMicTalkTests",
            dependencies: [
                "EVRuntime",
                .product(name: "EVClient", package: "EVClient"),
            ],
            path: "Tests/EVTests"
        ),
        .executableTarget(
            name: "EVAuthCheck",
            dependencies: ["EVAuth"],
            path: "Sources/EVAuthCheck",
            linkerSettings: [
                .linkedFramework("Contacts"),
                .linkedFramework("CoreBluetooth"),
                .linkedFramework("CoreLocation"),
                .linkedFramework("EventKit"),
                .linkedFramework("IOKit"),
                .linkedFramework("Speech"),
                .linkedFramework("UserNotifications"),
            ]
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
    ],
    swiftLanguageModes: [.v5]
)
