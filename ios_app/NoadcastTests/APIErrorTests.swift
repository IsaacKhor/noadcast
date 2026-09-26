//
//  APIErrorTests.swift
//  NoadcastTests
//
//  HTTP status / envelope → `APIError` mapping, Retry-After parsing, and the
//  retry classification the client's backoff loop relies on.
//

import Testing
import Foundation
@testable import Noadcast

struct APIErrorTests {

    private func body(_ json: String) -> Data {
        Data(json.utf8)
    }

    @Test func httpStatusesMapToTypedErrors() {
        #expect(APIError.from(statusCode: 401, retryAfterHeader: nil, body: nil) == .unauthorized)
        #expect(APIError.from(
            statusCode: 401,
            retryAfterHeader: nil,
            body: body(#"{"error": {"code": "unauthorized", "message": "Bad token"}}"#)
        ) == .unauthorized)
        #expect(APIError.from(
            statusCode: 404,
            retryAfterHeader: nil,
            body: body(#"{"error": {"code": "notFound", "message": "Episode 42 not found"}}"#)
        ) == .notFound(message: "Episode 42 not found"))
        #expect(APIError.from(
            statusCode: 409,
            retryAfterHeader: "5",
            body: body(#"{"error": {"code": "audioNotReady", "message": "Fetching audio"}, "jobId": 7}"#)
        ) == .audioNotReady(retryAfter: 5, jobId: 7))
        #expect(APIError.from(
            statusCode: 409,
            retryAfterHeader: "30",
            body: body(#"{"error": {"code": "audioEvicted", "message": "Re-downloading"}, "jobId": 8}"#)
        ) == .audioEvicted(retryAfter: 30, jobId: 8))
        #expect(APIError.from(
            statusCode: 410,
            retryAfterHeader: nil,
            body: body(#"{"error": {"code": "cursorExpired", "message": "Resync from 0"}}"#)
        ) == .cursorExpired)
        #expect(APIError.from(
            statusCode: 422,
            retryAfterHeader: nil,
            body: body(#"{"error": {"code": "invalidFeed", "message": "Not an RSS feed"}}"#)
        ) == .invalidRequest(code: "invalidFeed", message: "Not an RSS feed"))
        #expect(APIError.from(
            statusCode: 502,
            retryAfterHeader: nil,
            body: body(#"{"error": {"code": "upstreamFailed", "message": "Feed host timed out"}}"#)
        ) == .upstreamFailed(message: "Feed host timed out"))
        // FastAPI's default error shape still yields the message.
        #expect(APIError.from(
            statusCode: 500,
            retryAfterHeader: nil,
            body: body(#"{"detail": "Internal Server Error"}"#)
        ) == .server(status: 500, code: nil, message: "Internal Server Error"))
        // Non-JSON bodies map by status alone.
        #expect(APIError.from(statusCode: 503, retryAfterHeader: nil, body: body("<html>busy</html>"))
            == .server(status: 503, code: nil, message: nil))
    }

    @Test func rateLimitHonoursAnHTTPDateRetryAfter() {
        // Wed, 23 Sep 2026 12:00:00 GMT
        let now = Date(timeIntervalSince1970: 1_790_164_800)
        let error = APIError.from(
            statusCode: 429,
            retryAfterHeader: "Wed, 23 Sep 2026 12:02:00 GMT",
            body: body(#"{"error": {"code": "rateLimited", "message": "Slow down"}}"#),
            now: now
        )

        #expect(error == .rateLimited(retryAfter: 120))
        #expect(error.retryAfter == 120)
        #expect(error.isRetryable)
    }

    @Test func retryAfterParsesDeltaSecondsAndHTTPDates() {
        let now = Date(timeIntervalSince1970: 1_790_164_800)

        #expect(APIError.parseRetryAfter("5", now: now) == 5)
        #expect(APIError.parseRetryAfter(" 12 ", now: now) == 12)
        #expect(APIError.parseRetryAfter("-3", now: now) == 0)
        #expect(APIError.parseRetryAfter("Wed, 23 Sep 2026 12:02:00 GMT", now: now) == 120)
        #expect(APIError.parseRetryAfter("Wed, 23 Sep 2026 11:59:00 GMT", now: now) == 0)
        #expect(APIError.parseRetryAfter("soon", now: now) == nil)
        #expect(APIError.parseRetryAfter("", now: now) == nil)
        #expect(APIError.parseRetryAfter(nil, now: now) == nil)
    }

    @Test func onlyTransientFailuresAreRetryable() {
        #expect(APIError.rateLimited(retryAfter: nil).isRetryable)
        #expect(APIError.server(status: 500, code: nil, message: nil).isRetryable)
        #expect(APIError.server(status: 503, code: "unavailable", message: "busy").isRetryable)
        #expect(APIError.upstreamFailed(message: nil).isRetryable)
        #expect(APIError.transport(code: URLError.timedOut.rawValue, message: "timed out").isRetryable)
        #expect(APIError.transport(code: URLError.networkConnectionLost.rawValue, message: "lost").isRetryable)

        #expect(!APIError.unauthorized.isRetryable)
        #expect(!APIError.notFound(message: nil).isRetryable)
        #expect(!APIError.audioNotReady(retryAfter: 5, jobId: 7).isRetryable)
        #expect(!APIError.audioEvicted(retryAfter: nil, jobId: nil).isRetryable)
        #expect(!APIError.cursorExpired.isRetryable)
        #expect(!APIError.invalidRequest(code: "invalidFeed", message: nil).isRetryable)
        #expect(!APIError.server(status: 418, code: nil, message: nil).isRetryable)
        #expect(!APIError.transport(code: URLError.badURL.rawValue, message: "bad").isRetryable)
        #expect(!APIError.cancelled.isRetryable)
    }

    @Test func transportErrorsMapToCancelledOrTransport() {
        #expect(APIError.from(transportError: URLError(.cancelled)) == .cancelled)
        #expect(APIError.from(transportError: CancellationError()) == .cancelled)
        #expect(APIError.from(transportError: APIError.unauthorized) == .unauthorized)

        let timedOut = APIError.from(transportError: URLError(.timedOut))
        guard case .transport(let code, _) = timedOut else {
            Issue.record("Expected .transport, got \(timedOut)")
            return
        }
        #expect(code == URLError.timedOut.rawValue)
        #expect(timedOut.isRetryable)
        #expect(APIError.from(transportError: URLError(.notConnectedToInternet)).isOffline)
    }

    @Test func retryPolicyHonoursRetryAfterAndCapsBackoff() {
        let policy = RetryPolicy(maxAttempts: 4, baseDelay: 0.5, maxDelay: 8, maxRetryAfter: 30)

        #expect(policy.delay(beforeAttempt: 2, retryAfter: 5) == 5)
        #expect(policy.delay(beforeAttempt: 2, retryAfter: 120) == 30)
        let firstRetry = policy.delay(beforeAttempt: 2, retryAfter: nil)
        #expect(firstRetry >= 0.25 && firstRetry <= 0.5)
        let lateRetry = policy.delay(beforeAttempt: 20, retryAfter: nil)
        #expect(lateRetry >= 4 && lateRetry <= 8)
        let immediate = RetryPolicy(maxAttempts: 3, baseDelay: 0, maxDelay: 0, maxRetryAfter: 0)
        #expect(immediate.delay(beforeAttempt: 2, retryAfter: nil) == 0)
        #expect(immediate.delay(beforeAttempt: 2, retryAfter: 10) == 0)
    }

}
