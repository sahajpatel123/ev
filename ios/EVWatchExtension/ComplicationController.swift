import ClockKit
import EVClient

/// Real WatchKit complication data source replacing the old stub: the HUD
/// card payload renders as title + up to two lines via
/// ``WatchComplicationStub``.
final class ComplicationController: NSObject, CLKComplicationDataSource {
    private let fallbackCard = HUDCard(
        schemaVersion: "ev.hud.card.v1",
        generatedAt: "",
        title: "EV",
        body: "No active signals. EV is watching.",
        priority: 0
    )

    func getComplicationDescriptors(
        for complication: CLKComplication,
        withHandler handler: @escaping ([CLKComplicationDescriptor]) -> Void
    ) {
        handler([
            CLKComplicationDescriptor(
                identifier: "ev.hud.card",
                displayName: "EV HUD",
                supportedFamilies: [.modularSmall, .utilitarianSmall, .circularSmall]
            ),
        ])
    }

    func getCurrentTimelineEntry(
        for complication: CLKComplication,
        withHandler handler: @escaping (CLKComplicationTimelineEntry?) -> Void
    ) {
        let layout = WatchComplicationStub.render(fallbackCard)
        handler(CLKComplicationTimelineEntry(
            date: Date(),
            complicationTemplate: template(for: complication.family, layout: layout)
        ))
    }

    func getPlaceholderTemplate(
        for complication: CLKComplication,
        withHandler handler: @escaping (CLKComplicationTemplate?) -> Void
    ) {
        let layout = WatchComplicationStub.render(fallbackCard)
        handler(template(for: complication.family, layout: layout))
    }

    func getSupportedTimeTravelDirections(
        for complication: CLKComplication,
        withHandler handler: @escaping (CLKComplicationTimeTravelDirections) -> Void
    ) {
        handler([])
    }

    func getPrivacyBehavior(
        for complication: CLKComplication,
        withHandler handler: @escaping (CLKComplicationPrivacyBehavior) -> Void
    ) {
        handler(.showOnLockScreen)
    }

    private func template(
        for family: CLKComplicationFamily,
        layout: WatchComplicationStub.Layout
    ) -> CLKComplicationTemplate {
        switch family {
        case .modularSmall:
            let template = CLKComplicationTemplateModularSmallStackText()
            template.line1TextProvider = CLKSimpleTextProvider(text: layout.title)
            template.line2TextProvider = CLKSimpleTextProvider(text: layout.lines.first ?? "")
            return template
        case .utilitarianSmall:
            let template = CLKComplicationTemplateUtilitarianSmallFlat()
            template.textProvider = CLKSimpleTextProvider(
                text: "\(layout.title) \(layout.lines.first ?? "")"
            )
            return template
        default:
            let template = CLKComplicationTemplateCircularSmallSimpleText()
            template.textProvider = CLKSimpleTextProvider(text: layout.lines.first ?? layout.title)
            return template
        }
    }
}
