import Foundation
import Testing

@testable import EVClient

/// Wire-shape lock for the mesh advertisement record.
///
/// Regression: `lowPower` had no explicit snake_case CodingKey, so every
/// mesh-status/advertise decode threw `keyNotFound`, surfacing in-app as
/// "key 'low_power' not found" while voice kept working.
@Suite("Mesh advertisement coding")
struct MeshAdvertisementCodingTests {
    /// Exact backend shape from `app/everywhere/mesh.py::advertise`.
    private static let backendJSON = """
    {
        "device_id": "cead65bc-d483-4dbe-8271-7af0eb9f9324",
        "battery_percent": 82.0,
        "low_power": true,
        "capabilities": ["mesh"],
        "advertised_at": "2026-10-05T17:00:00+00:00"
    }
    """.data(using: .utf8)!

    @Test("decodes the backend snake_case payload")
    func decodesBackendPayload() throws {
        let record = try JSONDecoder().decode(
            EvieMeshAdvertisementRecord.self, from: Self.backendJSON
        )
        #expect(record.deviceId == "cead65bc-d483-4dbe-8271-7af0eb9f9324")
        #expect(record.batteryPercent == 82.0)
        #expect(record.lowPower == true)
        #expect(record.capabilities == ["mesh"])
    }

    @Test("missing low_power degrades to false instead of throwing")
    func missingLowPowerDegrades() throws {
        let legacy = """
        {
            "device_id": "abc",
            "battery_percent": null,
            "capabilities": [],
            "advertised_at": "2026-10-05T17:00:00+00:00"
        }
        """.data(using: .utf8)!
        let record = try JSONDecoder().decode(
            EvieMeshAdvertisementRecord.self, from: legacy
        )
        #expect(record.lowPower == false)
    }

    @Test("mesh status with advertisements decodes end to end")
    func meshStatusDecodes() throws {
        let status = """
        {
            "ok": true,
            "service_uuid": "9F5E2C7A-4B1D-4E63-8A0F-2C7D1B3E5A90",
            "zones": {"near": 1},
            "advertisements": {
                "cead65bc": {
                    "device_id": "cead65bc-d483-4dbe-8271-7af0eb9f9324",
                    "battery_percent": null,
                    "low_power": false,
                    "capabilities": ["mesh"],
                    "advertised_at": "2026-10-05T17:00:00+00:00"
                }
            },
            "mac_verbs": {},
            "sensors": {},
            "storage": "memory"
        }
        """.data(using: .utf8)!
        let decoded = try JSONDecoder().decode(EvieMeshStatus.self, from: status)
        #expect(decoded.advertisements.count == 1)
        #expect(decoded.advertisements.values.first?.lowPower == false)
    }
}
