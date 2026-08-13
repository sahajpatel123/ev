// swift-tools-version: 5.10

import PackageDescription

let package = Package(
    name: "EV",
    platforms: [
        .macOS(.v14),
    ],
    products: [
        .executable(name: "EV", targets: ["EV"]),
        .executable(name: "EVNotificationHelper", targets: ["EVNotificationHelper"]),
    ],
    dependencies: [
        .package(path: "../ios/EVClient"),
    ],
    targets: [
        .executableTarget(
            name: "EV",
            dependencies: [
                .product(name: "EVClient", package: "EVClient"),
                .product(name: "EVUI", package: "EVClient"),
            ],
            path: "Sources/EV",
            linkerSettings: [
                .linkedFramework("AVFoundation"),
                .linkedFramework("ApplicationServices"),
                .linkedFramework("Contacts"),
                .linkedFramework("CoreBluetooth"),
                .linkedFramework("CoreGraphics"),
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
    ]
)
