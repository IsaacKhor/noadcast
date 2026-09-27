import XCTest

final class QueueMarkPlayedTests: XCTestCase {
    @MainActor
    func testMarkPlayedButtonRemovesQueueRowLocalAudioAndQueuesRelease() {
        checkMarkPlayed(fullSwipe: false)
    }

    @MainActor
    func testFullTrailingSwipeMarksPlayedAndCleansUp() {
        checkMarkPlayed(fullSwipe: true)
    }

    @MainActor
    private func checkMarkPlayed(fullSwipe: Bool) {
        continueAfterFailure = false
        let app = XCUIApplication()
        app.launchArguments = ["--ui-test-queue"]
        app.launch()

        let episode = app.staticTexts["Queue episode 02"]
        XCTAssertTrue(episode.waitForExistence(timeout: 5))
        XCTAssertTrue(app.staticTexts["Fixture audio: present"].exists)
        XCTAssertTrue(app.staticTexts["Fixture release: none"].exists)

        let row = app.cells.containing(.staticText, identifier: "Queue episode 02").firstMatch
        if fullSwipe {
            let start = row.coordinate(withNormalizedOffset: CGVector(dx: 0.95, dy: 0.5))
            let end = row.coordinate(withNormalizedOffset: CGVector(dx: 0.05, dy: 0.5))
            start.press(forDuration: 0.05, thenDragTo: end)
        } else {
            row.swipeLeft()
            let action = app.buttons["Mark played"]
            XCTAssertTrue(action.waitForExistence(timeout: 3))
            action.tap()
        }

        let rowGone = XCTNSPredicateExpectation(predicate: NSPredicate(format: "exists == false"), object: episode)
        XCTAssertEqual(XCTWaiter.wait(for: [rowGone], timeout: 3), .completed)
        XCTAssertTrue(app.staticTexts["Fixture audio: removed"].waitForExistence(timeout: 3))
        XCTAssertTrue(app.staticTexts["Fixture release: pending"].waitForExistence(timeout: 3))
        XCTAssertTrue(app.staticTexts["Queue episode 01"].exists)
    }
}
