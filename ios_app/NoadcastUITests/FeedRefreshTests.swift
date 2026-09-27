import XCTest

final class FeedRefreshTests: XCTestCase {
    @MainActor
    func testRefreshButtonRecordsAcceptedRefresh() {
        continueAfterFailure = false
        let app = launchFixture()
        XCTAssertFalse(app.staticTexts["Refresh requested"].exists)
        app.buttons["Refresh all feeds"].tap()
        XCTAssertTrue(app.staticTexts["Refresh requested"].waitForExistence(timeout: 5))
        XCTAssertTrue(app.buttons["Refresh all feeds"].isEnabled)
    }

    @MainActor
    func testRefreshFailureShowsErrorWithoutClaimingSuccess() {
        continueAfterFailure = false
        let app = launchFixture(fails: true)
        app.buttons["Refresh all feeds"].tap()
        let alert = app.alerts["Couldn't refresh feeds"]
        XCTAssertTrue(alert.waitForExistence(timeout: 5))
        alert.buttons["OK"].tap()
        XCTAssertFalse(app.staticTexts["Refresh requested"].exists)
        XCTAssertTrue(app.buttons["Refresh all feeds"].isEnabled)
    }

    @MainActor
    private func launchFixture(fails: Bool = false) -> XCUIApplication {
        let app = XCUIApplication()
        app.launchArguments = ["--ui-test-refresh"] + (fails ? ["--refresh-fails"] : [])
        app.launch()
        XCTAssertTrue(app.staticTexts["Refresh fixture"].waitForExistence(timeout: 5))
        XCTAssertTrue(app.buttons["Refresh all feeds"].exists)
        return app
    }
}
