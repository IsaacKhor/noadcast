import Testing
@testable import Noadcast

@MainActor
struct RSSSummaryTextTests {
    @Test func preservesPlainProse() {
        let raw = "A show about R&D.\n\n5 < 10; café & conversation."
        #expect(RSSSummaryText.plainText(raw) == raw)
    }

    @Test func rendersFeedMarkupAndEntities() {
        let raw = #"<p>A <em>weekly</em> show.<br>Fish &amp; chips.</p><p>Visit <a href="https://example.com">our site</a>.</p>"#
        let text = RSSSummaryText.plainText(raw)
        #expect(text.contains("A weekly show."))
        #expect(text.contains("Fish & chips."))
        #expect(text.contains("Visit our site."))
        #expect(text.contains("\n"))
        #expect(!text.contains("<"))
        #expect(!text.contains("https://example.com"))
    }

    @Test func decodesEntitiesWithoutTags() {
        #expect(RSSSummaryText.plainText("Fish &amp; chips &#8212; café") == "Fish & chips — café")
    }
}
