import XCTest

final class StatusActionsTests: XCTestCase {
    @MainActor
    func testMarkPlayedDismissesPendingAndFailedRows() {
        continueAfterFailure = false
        let app = launchFixture()
        markPlayed("Status pending", in: app)
        markPlayed("Status failed", in: app)
        XCTAssertTrue(app.staticTexts["Status downloaded"].exists)
        XCTAssertTrue(app.staticTexts["Status legacy audio"].exists)
    }

    @MainActor
    func testMarkPlayedRemovesDownloadedFileWhileLegacyPlayedAudioRemainsVisible() {
        continueAfterFailure = false
        let app = launchFixture()
        let storage = app.cells.containing(.staticText, identifier: "On this iPhone").firstMatch
        XCTAssertTrue(storage.staticTexts["15 MB"].waitForExistence(timeout: 5))
        markPlayed("Status downloaded", in: app)
        XCTAssertTrue(app.staticTexts["Status legacy audio"].exists)
        XCTAssertTrue(storage.staticTexts["5 MB"].waitForExistence(timeout: 3))
    }

    @MainActor
    private func launchFixture() -> XCUIApplication {
        let app = XCUIApplication()
        app.launchArguments = ["--ui-test-status"]
        app.launch()
        XCTAssertTrue(app.staticTexts["Status pending"].waitForExistence(timeout: 5))
        return app
    }

    @MainActor
    private func markPlayed(_ title: String, in app: XCUIApplication) {
        let row = app.cells.containing(.staticText, identifier: title).firstMatch
        XCTAssertTrue(row.waitForExistence(timeout: 3))
        row.swipeLeft()
        let action = app.buttons["Mark \(title) played and stop its downloads and analysis"]
        XCTAssertTrue(action.waitForExistence(timeout: 3))
        action.tap()
        let disappeared = XCTNSPredicateExpectation(predicate: NSPredicate(format: "exists == false"), object: row)
        XCTAssertEqual(XCTWaiter.wait(for: [disappeared], timeout: 3), .completed)
    }
}
