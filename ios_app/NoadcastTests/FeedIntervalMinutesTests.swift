import Testing
@testable import Noadcast

struct FeedIntervalMinutesTests {
    @Test func acceptsAnyWholeMinuteInServerRange() {
        #expect(FeedIntervalMinutes.parse("1") == 1)
        #expect(FeedIntervalMinutes.parse(" 37 \n") == 37)
        #expect(FeedIntervalMinutes.parse("1440") == 1440)
    }

    @Test func rejectsValuesTheServerCannotSave() {
        for input in ["", " ", "0", "1441", "1.5", "+30", "-1", "1,440", "999999999999999999999999"] {
            #expect(FeedIntervalMinutes.parse(input) == nil)
        }
    }
}
