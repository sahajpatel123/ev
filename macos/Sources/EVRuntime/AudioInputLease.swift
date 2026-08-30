import Foundation

/// Which capture path currently owns the hardware input.
public enum AudioInputRole: String, Equatable, Sendable {
    case live
    case clip
}

/// Process-wide single owner for the microphone. Live duplex XOR clip PTT.
public enum AudioInputLease {
    private static let lock = NSLock()
    private static var owner: AudioInputRole?

    public static func currentOwner() -> AudioInputRole? {
        lock.lock()
        defer { lock.unlock() }
        return owner
    }

    /// Returns false when the other role already holds the input.
    public static func acquire(_ role: AudioInputRole) -> Bool {
        lock.lock()
        defer { lock.unlock() }
        if let owner, owner != role {
            return false
        }
        owner = role
        return true
    }

    public static func release(_ role: AudioInputRole) {
        lock.lock()
        defer { lock.unlock() }
        if owner == role {
            owner = nil
        }
    }

    public static func resetForTests() {
        lock.lock()
        owner = nil
        lock.unlock()
    }
}
