import XCTest

final class QueueScrollingTests: XCTestCase {
    @MainActor
    func testFullSwipeToTopPreservesScrollPosition() throws {
        try checkMoveToTop(fullSwipe: true)
    }

    @MainActor
    func testTopButtonPreservesScrollPosition() throws {
        try checkMoveToTop(fullSwipe: false)
    }

    @MainActor
    private func checkMoveToTop(fullSwipe: Bool) throws {
        continueAfterFailure = false
        XCUIDevice.shared.orientation = .portrait
        let app = XCUIApplication()
        app.launchArguments = ["--ui-test-queue"]
        app.launch()
        let first = app.staticTexts["Queue episode 01"]
        XCTAssertTrue(first.waitForExistence(timeout: 5))

        let target = app.staticTexts["Queue episode 20"]
        for _ in 0..<15 {
            if target.isHittable { break }
            app.swipeUp()
        }
        XCTAssertTrue(target.isHittable, "Fixture must be scrolled well away from the top")
        XCTAssertFalse(app.staticTexts["Latest episodes"].isHittable)

        // Pick another visible row to check that the viewport survives the move.
        let anchor = try XCTUnwrap((1...40).filter { $0 != 20 }.map {
            app.staticTexts[String(format: "Queue episode %02d", $0)]
        }.first { element in
            element.isHittable && element.frame.midY > 180
                && element.frame.midY < app.frame.height - 180
        })
        let previousY = anchor.frame.midY
        let row = app.cells.containing(.staticText, identifier: "Queue episode 20").firstMatch
        XCTAssertTrue(row.exists)
        if fullSwipe {
            // XCTest's default swipe can travel only half the row width,
            // revealing the action instead of committing a full swipe.
            let start = row.coordinate(withNormalizedOffset: CGVector(dx: 0.05, dy: 0.5))
            let end = row.coordinate(withNormalizedOffset: CGVector(dx: 0.95, dy: 0.5))
            start.press(forDuration: 0.05, thenDragTo: end)
        } else {
            let start = row.coordinate(withNormalizedOffset: CGVector(dx: 0.05, dy: 0.5))
            let end = row.coordinate(withNormalizedOffset: CGVector(dx: 0.4, dy: 0.5))
            start.press(forDuration: 0.05, thenDragTo: end)
            let top = app.buttons["Top"]
            XCTAssertTrue(top.waitForExistence(timeout: 3))
            top.tap()
        }

        let movedAway = NSPredicate(format: "hittable == false")
        expectation(for: movedAway, evaluatedWith: target)
        waitForExpectations(timeout: 3)
        XCTAssertTrue(anchor.isHittable, "Moving an episode must preserve the scrolled viewport")
        XCTAssertLessThan(abs(anchor.frame.midY - previousY), 120)
        XCTAssertFalse(app.staticTexts["Latest episodes"].isHittable, "List must not jump to its beginning")

        // Scroll deliberately to the beginning and verify the requested order.
        for _ in 0..<20 {
            if app.staticTexts["Latest episodes"].isHittable { break }
            app.swipeDown()
        }
        XCTAssertTrue(target.isHittable)
        XCTAssertTrue(first.isHittable)
        XCTAssertLessThan(target.frame.minY, first.frame.minY)
    }
}
