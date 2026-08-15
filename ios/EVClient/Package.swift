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
        .library(name: "EVUI", targets: ["EVUI"]),
        .executable(name: "EVClientCheck", targets: ["EVClientCheck"]),
        .executable(name: "EVUIValidate", targets: ["EVUIValidate"]),
    ],
    targets: [
        .target(
            name: "EVClientObjC",
            path: "Sources/EVObjCSupport",
            publicHeadersPath: "include",
            linkerSettings: [
                .linkedFramework("AVFAudio"),
            ]
        ),
        .target(
            name: "EVClient",
            dependencies: ["EVClientObjC"]
        ),
        .executableTarget(name: "EVClientCheck", dependencies: ["EVClient"]),
        .target(name: "EVUI", dependencies: ["EVClient"]),
        .executableTarget(name: "EVUIValidate", dependencies: ["EVUI"]),
    ]
)
