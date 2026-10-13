import XCTest

final class SetupTests: XCTestCase {
    func testSetupValidationAndBackgroundClearsPasswords() {
        XCUIDevice.shared.orientation = .landscapeRight
        let app = XCUIApplication(); app.launch()
        let setup = app.buttons["comms.setup"]
        XCTAssertTrue(setup.waitForExistence(timeout: 10)); setup.tap()
        let board = app.textFields["comms.Boardusername"]
        XCTAssertTrue(board.waitForExistence(timeout: 5))
        // Blank required usernames must not initiate network authentication.
        app.swipeUp()
        let connect = app.buttons["Connect"]
        XCTAssertTrue(connect.waitForExistence(timeout: 5)); connect.tap()
        XCTAssertTrue(app.staticTexts["Enter the required usernames without spaces or control characters."].waitForExistence(timeout: 5))
        app.swipeDown()
        let password = app.secureTextFields["comms.Boardpassword"]
        XCTAssertTrue(password.waitForExistence(timeout: 5)); password.tap(); password.typeText("local-ui-fixture")
        XCUIDevice.shared.press(.home)
        app.activate()
        XCTAssertTrue(password.waitForExistence(timeout: 5))
        XCTAssertEqual(password.value as? String, "Board password")
        let landscape = expectation(for: NSPredicate { _, _ in app.frame.width > app.frame.height }, evaluatedWith: app)
        wait(for: [landscape], timeout: 5)
        let screenshot = XCTAttachment(screenshot: app.screenshot())
        screenshot.name = "Shared native setup — simulator only"; screenshot.lifetime = .keepAlways
        add(screenshot)
    }
}
