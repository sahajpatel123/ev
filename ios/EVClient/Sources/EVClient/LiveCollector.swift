/// Live-data collector hooks for iOS (app-activity / screen-time style).
///
/// This is the client-side data model + upload path only: an app can feed a
/// ``LiveActivitySample`` from the DeviceActivity / screen-time APIs (or any
/// on-device activity tracker) and EV will store it as a `sensitive`
/// live-channel event.  Raw pixels, audio, and exact location never enter
/// this model.

import Foundation

/// One live event as sent to the ingestion endpoints.
public struct LiveEventCreate: Codable, Sendable, Equatable {
    public var eventType: String
    public var payload: [String: AnyCodable]
    public var occurredAt: String?
    public var deviceID: String?
    public var privacyLevel: String?

    public init(
        eventType: String,
        payload: [String: AnyCodable],
        occurredAt: String? = nil,
        deviceID: String? = nil,
        privacyLevel: String? = nil
    ) {
        self.eventType = eventType
        self.payload = payload
        self.occurredAt = occurredAt
        self.deviceID = deviceID
        self.privacyLevel = privacyLevel
    }
}

/// Batch envelope for ``POST /v1/live/events``.
public struct LiveBatchRequest: Codable, Sendable, Equatable {
    public var channel: String
    public var kind: String
    public var privacyLevel: String
    public var events: [LiveEventCreate]

    public init(
        channel: String,
        kind: String,
        privacyLevel: String,
        events: [LiveEventCreate]
    ) {
        self.channel = channel
        self.kind = kind
        self.privacyLevel = privacyLevel
        self.events = events
    }
}

/// One stored live event as returned by the ingestion endpoints.
public struct LiveEventOut: Codable, Sendable, Equatable {
    public let id: String
    public let channelId: String
    public let occurredAt: String
    public let ingestedAt: String
    public let eventType: String
    public let payload: [String: AnyCodable]
    public let deviceId: String?
    public let collector: String?
    public let privacyLevel: String
    public let sha256: String
    public let consumed: Bool
}

/// A live channel descriptor (useful for resolving ids before posting).
public struct LiveChannelOut: Codable, Sendable, Equatable {
    public let id: String
    public let name: String
    public let kind: String
    public let active: Bool
    public let privacyLevel: String
    public let metadata: [String: AnyCodable]?
    public let createdAt: String
    public let lastEventAt: String?
}

/// A derived, text-only activity observation from the on-device activity APIs.
///
/// Contains app/document names and a coarse category — never pixels, audio,
/// or exact location.
public struct LiveActivitySample: Codable, Sendable, Equatable {
    public var appName: String?
    public var documentName: String?
    public var category: String?
    public var startedAt: String?
    public var durationSeconds: Int?

    public init(
        appName: String? = nil,
        documentName: String? = nil,
        category: String? = nil,
        startedAt: String? = nil,
        durationSeconds: Int? = nil
    ) {
        self.appName = appName
        self.documentName = documentName
        self.category = category
        self.startedAt = startedAt
        self.durationSeconds = durationSeconds
    }
}

/// Hook for an app to produce app-activity samples (DeviceActivity, Focus,
/// screen-time style).  The app owns OS permission prompts and scheduling;
/// EV only receives the derived sample.
public protocol LiveActivityCollecting: Sendable {
    func currentActivity() async -> LiveActivitySample
}

/// Turns activity samples into `app-activity` live events and uploads them.
public struct LiveCollector: Sendable {
    public let client: EVAPIClient
    public let deviceID: String?

    public init(client: EVAPIClient, deviceID: String? = nil) {
        self.client = client
        self.deviceID = deviceID
    }

    /// Derived, text-only event: app/document names, never raw screen content.
    public func appActivityEvent(from sample: LiveActivitySample) -> LiveEventCreate {
        var payload: [String: AnyCodable] = [:]
        if let appName = sample.appName {
            payload["app"] = .string(appName)
        }
        if let documentName = sample.documentName {
            payload["document"] = .string(documentName)
        }
        if let category = sample.category {
            payload["category"] = .string(category)
        }
        if let durationSeconds = sample.durationSeconds {
            payload["duration_seconds"] = .number(Double(durationSeconds))
        }
        return LiveEventCreate(
            eventType: "focus_change",
            payload: payload,
            occurredAt: sample.startedAt,
            deviceID: deviceID,
            privacyLevel: "sensitive"
        )
    }

    /// Upload one or more samples; uses the batch endpoint unless a
    /// pre-created ``channelID`` is supplied, in which case it posts directly
    /// to ``/v1/live/channels/{id}/events``.
    public func upload(
        samples: [LiveActivitySample],
        channelID: String? = nil
    ) async throws -> [LiveEventOut] {
        let events = samples.map { appActivityEvent(from: $0) }
        if let channelID {
            return try await client.postLiveEvents(events, toChannel: channelID)
        }
        let batch = LiveBatchRequest(
            channel: "app-activity",
            kind: "app",
            privacyLevel: "sensitive",
            events: events
        )
        return try await client.postLiveBatch(batch)
    }

    public func upload(sample: LiveActivitySample, channelID: String? = nil) async throws -> [LiveEventOut] {
        try await upload(samples: [sample], channelID: channelID)
    }
}
