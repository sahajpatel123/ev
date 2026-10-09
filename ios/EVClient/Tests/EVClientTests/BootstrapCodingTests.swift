import Foundation
import Testing

@testable import EVClient

/// Wire-shape lock for idempotent device bootstrap.
///
/// The server echoes `client_device_id` on POST /v1/devices so the phone can
/// confirm which registry row it is bound to after a retry/reinstall returns
/// the existing row (200) instead of minting a duplicate (201).
@Suite("Bootstrap coding")
struct BootstrapCodingTests {
    /// Exact backend shape from `app/api/core.py::create_device`.
    private static let backendJSON = """
    {
        "device": {
            "id": "cead65bc-d483-4dbe-8271-7af0eb9f9324",
            "name": "phone-idfv",
            "created_at": "2026-10-08T12:00:00+00:00",
            "last_seen_at": null,
            "revoked_at": null,
            "capabilities": ["attention", "voice"],
            "trust_level": "device",
            "owner_id": null,
            "device_type": "phone",
            "platform": "apple",
            "client_device_id": "idfv-pro"
        },
        "token": "fresh-token"
    }
    """.data(using: .utf8)!

    private static func decoder() -> JSONDecoder {
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        return decoder
    }

    @Test("decodes the stable install id echo")
    func decodesClientDeviceId() throws {
        let decoded = try Self.decoder().decode(DeviceCreateResponse.self, from: Self.backendJSON)
        #expect(decoded.device.id == "cead65bc-d483-4dbe-8271-7af0eb9f9324")
        #expect(decoded.device.clientDeviceId == "idfv-pro")
        #expect(decoded.token == "fresh-token")
    }

    @Test("tolerates rows created before the stable id existed")
    func toleratesMissingClientDeviceId() throws {
        let legacy = """
        {"device": {"id": "old-row", "name": "Old"}, "token": "t"}
        """.data(using: .utf8)!
        let decoded = try Self.decoder().decode(DeviceCreateResponse.self, from: legacy)
        #expect(decoded.device.id == "old-row")
        #expect(decoded.device.clientDeviceId == nil)
    }
}
