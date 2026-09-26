import Foundation

/// Everything the Noadcast API client can throw. `Equatable` so tests (and
/// retry logic) can match cases directly.
nonisolated enum APIError: Error, Equatable, Sendable, LocalizedError {
    /// No server address configured.
    case notConfigured
    case invalidURL
    /// `URLError` from the transport (no HTTP response).
    case transport(code: Int, message: String)
    /// 401 — the token is missing or wrong. Never retried.
    case unauthorized
    case notFound(message: String?)
    /// 410 `cursorExpired` — the delta cursor predates tombstone retention.
    case cursorExpired
    /// 409 `audioNotReady` — the server does not have the audio yet; a
    /// priority download has been enqueued.
    case audioNotReady(retryAfter: TimeInterval?, jobId: Int?)
    /// 409 `audioEvicted` — the server deleted its copy; re-download enqueued.
    case audioEvicted(retryAfter: TimeInterval?, jobId: Int?)
    case rateLimited(retryAfter: TimeInterval?)
    /// 400 / 422 (`invalidRequest`, `invalidFeed`, …).
    case invalidRequest(code: String?, message: String?)
    /// 502 `upstreamFailed` — the podcast host failed, not the server.
    case upstreamFailed(message: String?)
    /// Any other non-2xx.
    case server(status: Int, code: String?, message: String?)
    case decoding(String)
    case invalidResponse
    case cancelled

    // MARK: - Classification

    /// Worth retrying with backoff: transport glitches, 429, and 5xx.
    var isRetryable: Bool {
        switch self {
        case .transport(let code, _):
            return Self.retryableTransportCodes.contains(code)
        case .rateLimited:
            return true
        case .upstreamFailed:
            return true
        case .server(let status, _, _):
            return (500...599).contains(status) || status == 408
        case .notConfigured, .invalidURL, .unauthorized, .notFound, .cursorExpired,
             .audioNotReady, .audioEvicted, .invalidRequest, .decoding,
             .invalidResponse, .cancelled:
            return false
        }
    }

    var retryAfter: TimeInterval? {
        switch self {
        case .rateLimited(let retryAfter),
             .audioNotReady(let retryAfter, _),
             .audioEvicted(let retryAfter, _):
            return retryAfter
        default:
            return nil
        }
    }

    /// The device looks offline (as opposed to the server misbehaving).
    var isOffline: Bool {
        guard case .transport(let code, _) = self else { return false }
        return code == URLError.notConnectedToInternet.rawValue
            || code == URLError.networkConnectionLost.rawValue
            || code == URLError.dataNotAllowed.rawValue
            || code == URLError.internationalRoamingOff.rawValue
    }

    var isAudioUnavailable: Bool {
        switch self {
        case .audioNotReady, .audioEvicted: true
        default: false
        }
    }

    var errorDescription: String? {
        switch self {
        case .notConfigured:
            return "No Noadcast server is configured."
        case .invalidURL:
            return "The server address is not a valid URL."
        case .transport(let code, let message):
            switch code {
            case URLError.cannotFindHost.rawValue, URLError.dnsLookupFailed.rawValue:
                return "Can't find the server. Is Tailscale connected?"
            case URLError.cannotConnectToHost.rawValue:
                return "The server refused the connection. Is it running?"
            case URLError.timedOut.rawValue:
                return "The server took too long to respond."
            case URLError.notConnectedToInternet.rawValue:
                return "You're offline."
            default:
                return message
            }
        case .unauthorized:
            return "The server rejected the access token."
        case .notFound(let message):
            return message ?? "Not found on the server."
        case .cursorExpired:
            return "The sync cursor expired; a full resync is needed."
        case .audioNotReady:
            return "The server is still fetching this episode's audio."
        case .audioEvicted:
            return "The server removed this episode's audio and is fetching it again."
        case .rateLimited:
            return "The server is busy. Try again shortly."
        case .invalidRequest(_, let message):
            return message ?? "The server rejected the request."
        case .upstreamFailed(let message):
            return message ?? "The podcast's host could not be reached."
        case .server(let status, _, let message):
            return message ?? "Server returned HTTP \(status)."
        case .decoding(let detail):
            return "Unexpected response from the server (\(detail))."
        case .invalidResponse:
            return "Unexpected response from the server."
        case .cancelled:
            return "Cancelled."
        }
    }

    // MARK: - Mapping

    static let retryableTransportCodes: Set<Int> = [
        URLError.timedOut.rawValue,
        URLError.networkConnectionLost.rawValue,
        URLError.cannotConnectToHost.rawValue,
        URLError.cannotFindHost.rawValue,
        URLError.dnsLookupFailed.rawValue,
        URLError.notConnectedToInternet.rawValue,
        URLError.resourceUnavailable.rawValue,
        URLError.badServerResponse.rawValue,
        URLError.secureConnectionFailed.rawValue
    ]

    /// Maps a non-2xx HTTP response. `body` is the JSON error envelope
    /// `{"error": {"code", "message"}, "jobId"?}` when the server produced
    /// it; anything else (FastAPI's `{"detail": …}`, HTML, empty) still maps
    /// by status.
    static func from(statusCode: Int, retryAfterHeader: String?, body: Data?, now: Date = Date()) -> APIError {
        let envelope = body.flatMap { try? JSONDecoder().decode(APIErrorEnvelope.self, from: $0) }
        let code = envelope?.error?.code
        let message = envelope?.error?.message ?? envelope?.detailMessage
        let retryAfter = parseRetryAfter(retryAfterHeader, now: now)
        switch statusCode {
        case 401, 403 where code == "unauthorized":
            return .unauthorized
        case 404:
            return .notFound(message: message)
        case 409:
            if code == "audioEvicted" {
                return .audioEvicted(retryAfter: retryAfter, jobId: envelope?.jobId)
            }
            if code == "audioNotReady" || code == nil {
                return .audioNotReady(retryAfter: retryAfter, jobId: envelope?.jobId)
            }
            return .server(status: statusCode, code: code, message: message)
        case 410:
            return .cursorExpired
        case 429:
            return .rateLimited(retryAfter: retryAfter)
        case 400, 422:
            return .invalidRequest(code: code, message: message)
        case 502 where code == nil || code == "upstreamFailed":
            return .upstreamFailed(message: message)
        default:
            return .server(status: statusCode, code: code, message: message)
        }
    }

    static func from(transportError error: Error) -> APIError {
        if let apiError = error as? APIError {
            return apiError
        }
        if error is CancellationError {
            return .cancelled
        }
        if let urlError = error as? URLError {
            if urlError.code == .cancelled {
                return .cancelled
            }
            return .transport(code: urlError.code.rawValue, message: urlError.localizedDescription)
        }
        let ns = error as NSError
        if ns.domain == NSURLErrorDomain {
            if ns.code == URLError.cancelled.rawValue {
                return .cancelled
            }
            return .transport(code: ns.code, message: ns.localizedDescription)
        }
        return .transport(code: URLError.unknown.rawValue, message: error.localizedDescription)
    }

    /// `Retry-After` is either delta-seconds or an HTTP-date (RFC 9110 §10.2.3).
    static func parseRetryAfter(_ value: String?, now: Date = Date()) -> TimeInterval? {
        guard let raw = value?.trimmingCharacters(in: .whitespaces), !raw.isEmpty else { return nil }
        if let seconds = TimeInterval(raw), seconds.isFinite {
            return max(0, seconds)
        }
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.timeZone = TimeZone(identifier: "GMT")
        formatter.dateFormat = "EEE, dd MMM yyyy HH:mm:ss zzz"
        if let date = formatter.date(from: raw) {
            return max(0, date.timeIntervalSince(now))
        }
        return nil
    }
}

/// `{"error": {"code": "…", "message": "…"}, "jobId": n}` — plus FastAPI's
/// default `{"detail": "…"}` shape as a fallback.
nonisolated struct APIErrorEnvelope: Decodable, Sendable {
    nonisolated struct Body: Decodable, Sendable {
        let code: String?
        let message: String?
    }

    let error: Body?
    let jobId: Int?
    let detailMessage: String?

    nonisolated enum CodingKeys: String, CodingKey {
        case error
        case jobId
        case detail
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        error = try? container.decodeIfPresent(Body.self, forKey: .error)
        jobId = try? container.decodeIfPresent(Int.self, forKey: .jobId)
        detailMessage = try? container.decodeIfPresent(String.self, forKey: .detail)
    }
}
