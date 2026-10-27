import AppKit
import Foundation

// Placeholder island content for previews and layout checks ONLY.
// CLI 2 replaces these with the real HUD views (plan steps, captions,
// confirm card) through the `IslandContent` protocol. Nothing here is
// wired to actions: buttons are layout-only.

private func label(_ text: String, size: CGFloat = 13, dim: Bool = false) -> NSTextField {
    let t = NSTextField(labelWithString: text)
    t.font = NSFont.systemFont(ofSize: size)
    t.textColor = dim ? .secondaryLabelColor : .labelColor
    t.lineBreakMode = .byTruncatingTail
    return t
}

/// Placeholder plan-steps list ("Texting Sam", 4 steps).
public final class PlaceholderPlanSteps: IslandContent {
    public let view: NSView
    public let contentHeight: CGFloat
    public let contentID = "plan-steps"

    public init(steps: [(label: String, status: String)] = [
        ("Find Sam's number", "done"),
        ("Draft the message", "done"),
        ("Confirm with you", "active"),
        ("Send", "queued"),
    ]) {
        let stack = NSStackView()
        stack.orientation = .vertical
        stack.spacing = 4
        stack.alignment = .leading
        for step in steps {
            let row = NSStackView()
            row.orientation = .horizontal
            row.alignment = .centerY
            row.spacing = 6
            let dot = NSView(frame: NSRect(x: 0, y: 0, width: 8, height: 8))
            dot.wantsLayer = true
            dot.layer?.cornerRadius = 4
            dot.widthAnchor.constraint(equalToConstant: 8).isActive = true
            dot.heightAnchor.constraint(equalToConstant: 8).isActive = true
            switch step.status {
            case "done":
                dot.layer?.backgroundColor = NSColor.systemGreen.cgColor
            case "active":
                dot.layer?.backgroundColor = NSColor.systemBlue.cgColor
            default:
                dot.layer?.backgroundColor = NSColor.tertiaryLabelColor.cgColor
            }
            row.addArrangedSubview(dot)
            row.addArrangedSubview(label(step.label, dim: step.status == "queued"))
            stack.addArrangedSubview(row)
        }
        view = stack
        contentHeight = CGFloat(steps.count * 22 + 8)
    }
}

/// Placeholder live caption line.
public final class PlaceholderCaption: IslandContent {
    public let view: NSView
    public let contentHeight: CGFloat = 48
    public let contentID = "caption"

    public init(text: String = "Found Sam. Drafting your message.") {
        let t = NSTextField(wrappingLabelWithString: text)
        t.font = NSFont.systemFont(ofSize: 14)
        t.textColor = .labelColor
        t.maximumNumberOfLines = 2
        view = t
    }
}

/// Placeholder confirm card (layout only — buttons are NOT wired).
public final class PlaceholderConfirmCard: IslandContent {
    public let view: NSView
    public let contentHeight: CGFloat = 120
    public let contentID = "confirm"

    public init(prompt: String = "Send via Messages to Sam (+1 555-0100)?") {
        let stack = NSStackView()
        stack.orientation = .vertical
        stack.spacing = 8
        stack.alignment = .leading
        stack.addArrangedSubview(label(prompt))
        stack.addArrangedSubview(label("Preview placeholder — wired by the presence HUD.", size: 11, dim: true))
        let row = NSStackView()
        row.orientation = .horizontal
        row.spacing = 8
        let deny = NSButton(title: "Deny", target: nil, action: nil)
        deny.bezelStyle = .rounded
        let approve = NSButton(title: "Approve", target: nil, action: nil)
        approve.bezelStyle = .rounded
        approve.keyEquivalent = "\r"
        row.addArrangedSubview(deny)
        row.addArrangedSubview(approve)
        stack.addArrangedSubview(row)
        view = stack
    }
}
