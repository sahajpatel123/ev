// swift-tools-version:5.10
import PackageDescription
import Foundation

let packageRoot = URL(fileURLWithPath: #filePath).deletingLastPathComponent().path

let package = Package(
    name: "evvision",
    platforms: [.macOS(.v14)],
    products: [
        .executable(name: "evvision", targets: ["evvision"])
    ],
    targets: [
        .executableTarget(
            name: "evvision",
            path: "Sources/evvision",
            exclude: ["Info.plist"],
            linkerSettings: [
                .unsafeFlags([
                    "-Xlinker", "-sectcreate",
                    "-Xlinker", "__TEXT",
                    "-Xlinker", "__info_plist",
                    "-Xlinker", "\(packageRoot)/Sources/evvision/Info.plist",
                ])
            ]
        )
    ]
)
