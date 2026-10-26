// swift-tools-version: 6.0

import PackageDescription

let package = Package(
    name: "EvieOrb",
    platforms: [
        .macOS(.v14),
    ],
    products: [
        .library(name: "EvieOrb", targets: ["EvieOrb"]),
    ],
    targets: [
        .target(
            name: "EvieOrb",
            path: "Sources/EvieOrb",
            resources: [
                .copy("Shaders"),
            ],
            linkerSettings: [
                .linkedFramework("Metal"),
                .linkedFramework("QuartzCore"),
                .linkedFramework("Accelerate"),
            ]
        ),
        .executableTarget(
            name: "EvieOrbPreviewTool",
            dependencies: ["EvieOrb"],
            path: "Sources/EvieOrbPreviewTool"
        ),
        .executableTarget(
            name: "OrbPreviewApp",
            dependencies: ["EvieOrb"],
            path: "Sources/OrbPreviewApp",
            linkerSettings: [
                .linkedFramework("AVFoundation"),
            ]
        ),
        .executableTarget(
            name: "EvieOrbCheck",
            dependencies: ["EvieOrb"],
            path: "Sources/EvieOrbCheck"
        ),
    ],
    swiftLanguageModes: [.v5]
)
