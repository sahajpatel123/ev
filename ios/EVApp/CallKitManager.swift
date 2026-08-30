#if canImport(CallKit)
import CallKit
import Foundation

/// Authored CallKit placement for a personal VoIP-capable app. The app
/// declares the `voip` background mode; this provider reports an outgoing
/// call when the action is requested. Unverified until Xcode is available.
final class EVCallKitManager: NSObject, CXProviderDelegate {
    private let provider: CXProvider
    private let callController = CXCallController()

    override init() {
        let configuration = CXProviderConfiguration()
        configuration.supportsVideo = false
        configuration.maximumCallGroups = 1
        configuration.maximumCallsPerCallGroup = 1
        provider = CXProvider(configuration: configuration)
        super.init()
        provider.setDelegate(self, queue: nil)
    }

    func startCall(destination: String) {
        let handle = CXHandle(type: .phoneNumber, value: destination)
        let action = CXStartCallAction(call: UUID(), handle: handle)
        callController.requestTransaction(with: action) { _ in }
    }

    func providerDidReset(_ provider: CXProvider) {}

    func provider(_ provider: CXProvider, perform action: CXStartCallAction) {
        provider.reportOutgoingCall(with: action.callUUID, startedConnectingAt: nil)
        provider.reportOutgoingCall(with: action.callUUID, connectedAt: Date())
        action.fulfill()
    }
}
#endif
