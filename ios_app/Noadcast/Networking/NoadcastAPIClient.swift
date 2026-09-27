import Foundation
import os

/// Jittered exponential backoff for transport errors, 429, and 5xx.
/// `Retry-After` is honoured exactly (capped) when the server sends it.
nonisolated struct RetryPolicy: Sendable, Equatable {
    var maxAttempts: Int
    var baseDelay: TimeInterval
    var maxDelay: TimeInterval
    var maxRetryAfter: TimeInterval

    init(maxAttempts: Int = 3, baseDelay: TimeInterval = 0.5, maxDelay: TimeInterval = 8, maxRetryAfter: TimeInterval = 30) {
        self.maxAttempts = maxAttempts
        self.baseDelay = baseDelay
        self.maxDelay = maxDelay
        self.maxRetryAfter = maxRetryAfter
    }

    static let standard = RetryPolicy()
    /// Single attempt — for pollers that will simply try again next tick.
    static let once = RetryPolicy(maxAttempts: 1)

    /// Delay before attempt number `attempt` (2 = first retry).
    func delay(beforeAttempt attempt: Int, retryAfter: TimeInterval?) -> TimeInterval {
        if let retryAfter {
            return min(max(0, retryAfter), maxRetryAfter)
        }
        let exponent = Double(max(0, attempt - 2))
        let ceiling = min(maxDelay, baseDelay * pow(2, exponent))
        guard ceiling > 0 else { return 0 }
        return ceiling * Double.random(in: 0.5...1.0)
    }
}

/// Client for docs/API.md.
///
/// * Injectable `URLSession` (tests use a `URLProtocol` stub) and endpoint
///   provider (base URL from `UserDefaults`, token from the Keychain).
/// * Retries transport errors / 429 / 5xx with jittered backoff; a 401 is
///   reported once through `authStateHandler` (the auth banner) and never
///   retried, so a bad token cannot cause a retry storm.
/// * Never logs the token or signed audio URLs.
actor NoadcastAPIClient {
    static let shared: NoadcastAPIClient = NoadcastAPIClient(
        session: NoadcastAPIClient.makeDefaultSession(),
        endpointProvider: { APIConfiguration.currentEndpoint },
        retryPolicy: .standard,
        authStateHandler: { failed in
            Task { @MainActor in
                SyncService.shared.setAuthFailed(failed)
            }
        }
    )

    private let urlSession: URLSession
    private let endpointProvider: @Sendable () -> APIConfiguration.Endpoint?
    private let retryPolicy: RetryPolicy
    private let authStateHandler: (@Sendable (Bool) -> Void)?
    private let decoder: JSONDecoder
    /// Last auth state reported, so the handler fires on transitions only.
    private var reportedAuthFailure: Bool?

    init(
        session: URLSession,
        endpointProvider: @escaping @Sendable () -> APIConfiguration.Endpoint?,
        retryPolicy: RetryPolicy = .standard,
        authStateHandler: (@Sendable (Bool) -> Void)? = nil
    ) {
        self.urlSession = session
        self.endpointProvider = endpointProvider
        self.retryPolicy = retryPolicy
        self.authStateHandler = authStateHandler
        self.decoder = APIJSON.makeDecoder()
    }

    nonisolated static func makeDefaultSession() -> URLSession {
        let configuration = URLSessionConfiguration.default
        configuration.timeoutIntervalForRequest = 30
        configuration.timeoutIntervalForResource = 120
        configuration.waitsForConnectivity = false
        // ETags are handled explicitly (`/jobs/active`); a URL cache would
        // turn the server's 304s back into 200s.
        configuration.urlCache = nil
        configuration.requestCachePolicy = .reloadIgnoringLocalCacheData
        configuration.httpAdditionalHeaders = ["Accept": "application/json"]
        return URLSession(configuration: configuration)
    }

    // MARK: - Session

    /// `GET /health` (no auth). Version, `instanceId`, capabilities.
    func health() async throws -> HealthDTO {
        let request = try makeRequest("GET", "/health", authenticated: false, timeout: 10)
        let (data, _) = try await send(request, authenticated: false, policy: .once)
        return try decode(HealthDTO.self, data)
    }

    /// `GET /api/v1/session` — cheap token check.
    func sessionInfo() async throws -> SessionDTO {
        let request = try makeRequest("GET", "/api/v1/session", timeout: 10)
        let (data, _) = try await send(request, policy: .once)
        return try decode(SessionDTO.self, data)
    }

    // MARK: - Sync

    func sync(since: Int, limit: Int) async throws -> SyncPageDTO {
        let request = try makeRequest(
            "GET",
            "/api/v1/sync",
            query: [
                URLQueryItem(name: "since", value: String(since)),
                URLQueryItem(name: "limit", value: String(limit))
            ],
            timeout: 60
        )
        let (data, _) = try await send(request)
        return try decode(SyncPageDTO.self, data)
    }

    /// `GET /api/v1/jobs/active` with `If-None-Match`. Single attempt: the
    /// poller simply tries again on its next tick.
    func activeJobs(ifNoneMatch etag: String?) async throws -> ActiveJobsResult {
        var request = try makeRequest("GET", "/api/v1/jobs/active", timeout: 15)
        if let etag {
            request.setValue(etag, forHTTPHeaderField: "If-None-Match")
        }
        let (data, response) = try await send(request, policy: .once, acceptNotModified: true)
        if response.statusCode == 304 {
            return .notModified
        }
        let dto = try decode(ActiveJobsDTO.self, data)
        return .updated(items: dto.items, etag: response.value(forHTTPHeaderField: "ETag"))
    }

    // MARK: - Podcasts

    /// `POST /api/v1/podcasts` — idempotent (200 when already subscribed).
    func subscribe(feedURL: String) async throws -> SubscribeResponseDTO {
        let request = try makeRequest(
            "POST",
            "/api/v1/podcasts",
            jsonBody: ["feedUrl": feedURL],
            timeout: 45
        )
        let (data, _) = try await send(request)
        return try decode(SubscribeResponseDTO.self, data)
    }

    /// `PATCH /api/v1/podcasts/{id}` → `200 Podcast`.
    func updatePodcast(id: Int, adAnalysisEnabled: Bool? = nil, autoProcessEnabled: Bool? = nil) async throws -> PodcastDTO {
        var body: [String: Any] = [:]
        if let adAnalysisEnabled { body["adAnalysisEnabled"] = adAnalysisEnabled }
        if let autoProcessEnabled { body["autoProcessEnabled"] = autoProcessEnabled }
        let request = try makeRequest("PATCH", "/api/v1/podcasts/\(id)", jsonBody: body)
        let (data, _) = try await send(request)
        if let podcast = try? decoder.decode(PodcastDTO.self, from: data) {
            return podcast
        }
        // Tolerate a `{"podcast": …}` envelope as well.
        return try decode(SubscribeResponseDTO.self, data).podcast
    }

    /// `DELETE /api/v1/podcasts/{id}`. A 404 means it is already gone.
    func deletePodcast(id: Int) async throws {
        let request = try makeRequest("DELETE", "/api/v1/podcasts/\(id)")
        do {
            _ = try await send(request)
        } catch APIError.notFound {
            return
        }
    }

    func refreshPodcast(id: Int) async throws {
        let request = try makeRequest("POST", "/api/v1/podcasts/\(id)/refresh")
        _ = try await send(request)
    }

    func refreshAll() async throws {
        let request = try makeRequest("POST", "/api/v1/refresh")
        _ = try await send(request)
    }

    // MARK: - Episodes

    func episode(id: Int) async throws -> EpisodeDTO {
        let request = try makeRequest("GET", "/api/v1/episodes/\(id)", timeout: 15)
        let (data, _) = try await send(request)
        return try decode(EpisodeDTO.self, data)
    }

    /// `POST /api/v1/episodes/{id}/process` — ensures the audio is on the
    /// server (priority download) and analysed. Idempotent; `nil` job id
    /// when nothing is left to do.
    @discardableResult
    func process(episodeID: Int) async throws -> Int? {
        let request = try makeRequest("POST", "/api/v1/episodes/\(episodeID)/process")
        let (data, _) = try await send(request)
        return (try? decoder.decode(JobResponseDTO.self, from: data))?.jobId
    }

    @discardableResult
    func reanalyze(episodeID: Int, retranscribe: Bool = false) async throws -> Int? {
        var body: [String: Any] = [:]
        if retranscribe { body["retranscribe"] = true }
        let request = try makeRequest("POST", "/api/v1/episodes/\(episodeID)/reanalyze", jsonBody: body)
        let (data, _) = try await send(request)
        return (try? decoder.decode(JobResponseDTO.self, from: data))?.jobId
    }

    // MARK: - Audio

    /// `POST /api/v1/episodes/{id}/audio-url` → a signed stream URL (for
    /// AVPlayer, which cannot send an Authorization header). Throws
    /// `.audioNotReady` / `.audioEvicted` (409) when the server lacks the
    /// audio; a priority download has then already been enqueued.
    func audioURL(episodeID: Int) async throws -> SignedAudioURL {
        let request = try makeRequest("POST", "/api/v1/episodes/\(episodeID)/audio-url", timeout: 15)
        let (data, _) = try await send(request)
        let dto = try decode(AudioURLDTO.self, data)
        let endpoint = try currentEndpoint()
        if let path = dto.path, let url = endpoint.resolve(pathAndQuery: path) {
            return SignedAudioURL(url: url, expiresAt: dto.expiresAt)
        }
        if let absolute = dto.url, let url = URL(string: absolute) {
            return SignedAudioURL(url: url, expiresAt: dto.expiresAt)
        }
        throw APIError.invalidResponse
    }

    /// `DELETE /api/v1/episodes/{id}/audio?reason=…` — the retention release.
    func releaseAudio(episodeID: Int, reason: AudioReleaseReason) async throws {
        let request = try makeRequest(
            "DELETE",
            "/api/v1/episodes/\(episodeID)/audio",
            query: [URLQueryItem(name: "reason", value: reason.rawValue)]
        )
        _ = try await send(request)
    }

    /// Authenticated `GET …/audio` for the background download session.
    /// Uses the bearer header rather than a signed URL so an expiring
    /// signature can never poison persisted resume data.
    nonisolated static func audioDownloadRequest(episodeID: Int, endpoint: APIConfiguration.Endpoint) -> URLRequest? {
        guard let url = endpoint.url(path: "/api/v1/episodes/\(episodeID)/audio") else { return nil }
        var request = URLRequest(url: url)
        request.httpMethod = "GET"
        if let token = endpoint.token, !token.isEmpty {
            request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        }
        return request
    }

    // MARK: - Settings, usage, OPML

    func settings() async throws -> ServerSettingsDTO {
        let request = try makeRequest("GET", "/api/v1/settings")
        let (data, _) = try await send(request)
        return try decode(ServerSettingsDTO.self, data)
    }

    func updateSettings(adAnalysisEnabled: Bool? = nil, autoProcessEnabled: Bool? = nil, classifierModel: String? = nil) async throws -> ServerSettingsDTO {
        var body: [String: Any] = [:]
        if let adAnalysisEnabled { body["adAnalysisEnabled"] = adAnalysisEnabled }
        if let autoProcessEnabled { body["autoProcessEnabled"] = autoProcessEnabled }
        if let classifierModel {
            body["classifier"] = "openrouter"
            body["classifierModel"] = classifierModel
        }
        let request = try makeRequest("PATCH", "/api/v1/settings", jsonBody: body)
        let (data, _) = try await send(request)
        return try decode(ServerSettingsDTO.self, data)
    }

    /// `GET /api/v1/usage?days=` — `nil` when the server does not implement
    /// it (404), so the UI can hide the section.
    func usage(days: Int = 30) async throws -> UsageDTO? {
        let request = try makeRequest(
            "GET",
            "/api/v1/usage",
            query: [URLQueryItem(name: "days", value: String(days))]
        )
        do {
            let (data, _) = try await send(request)
            return try decode(UsageDTO.self, data)
        } catch APIError.notFound {
            return nil
        }
    }

    /// `POST /api/v1/opml` with the OPML document as the body.
    func importOPML(_ opml: Data) async throws -> OPMLImportResultDTO {
        let request = try makeRequest(
            "POST",
            "/api/v1/opml",
            rawBody: opml,
            contentType: "text/x-opml; charset=utf-8",
            timeout: 60
        )
        let (data, _) = try await send(request)
        return try decode(OPMLImportResultDTO.self, data)
    }

    // MARK: - Plumbing

    private func currentEndpoint() throws -> APIConfiguration.Endpoint {
        guard let endpoint = endpointProvider() else { throw APIError.notConfigured }
        return endpoint
    }

    private func makeRequest(
        _ method: String,
        _ path: String,
        query: [URLQueryItem] = [],
        jsonBody: [String: Any]? = nil,
        rawBody: Data? = nil,
        contentType: String? = nil,
        authenticated: Bool = true,
        timeout: TimeInterval? = nil
    ) throws -> URLRequest {
        let endpoint = try currentEndpoint()
        guard let url = endpoint.url(path: path, query: query) else { throw APIError.invalidURL }
        var request = URLRequest(url: url)
        request.httpMethod = method
        if let timeout {
            request.timeoutInterval = timeout
        }
        if authenticated, let token = endpoint.token, !token.isEmpty {
            request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        }
        if let jsonBody {
            request.httpBody = try JSONSerialization.data(withJSONObject: jsonBody)
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        } else if let rawBody {
            request.httpBody = rawBody
            if let contentType {
                request.setValue(contentType, forHTTPHeaderField: "Content-Type")
            }
        }
        return request
    }

    /// One attempt, mapped to a `Result` so the retry loop stays linear.
    private func performOnce(
        _ request: URLRequest,
        acceptNotModified: Bool
    ) async -> Result<(Data, HTTPURLResponse), APIError> {
        do {
            let (data, response) = try await urlSession.data(for: request)
            guard let http = response as? HTTPURLResponse else {
                return .failure(.invalidResponse)
            }
            if (200..<300).contains(http.statusCode) || (acceptNotModified && http.statusCode == 304) {
                return .success((data, http))
            }
            return .failure(APIError.from(
                statusCode: http.statusCode,
                retryAfterHeader: http.value(forHTTPHeaderField: "Retry-After"),
                body: data
            ))
        } catch {
            return .failure(APIError.from(transportError: error))
        }
    }

    private func send(
        _ request: URLRequest,
        authenticated: Bool = true,
        policy: RetryPolicy? = nil,
        acceptNotModified: Bool = false
    ) async throws -> (Data, HTTPURLResponse) {
        let policy = policy ?? retryPolicy
        let label = "\(request.httpMethod ?? "GET") \(request.url?.path ?? "?")"
        var attempt = 1
        while true {
            if Task.isCancelled {
                throw APIError.cancelled
            }
            switch await performOnce(request, acceptNotModified: acceptNotModified) {
            case .success(let value):
                if authenticated {
                    noteAuthState(failed: false)
                }
                return value
            case .failure(let error):
                if case .unauthorized = error {
                    noteAuthState(failed: true)
                    Log.network.notice("\(label, privacy: .public) → 401; not retrying")
                    throw error
                }
                guard error.isRetryable, attempt < max(1, policy.maxAttempts) else {
                    throw error
                }
                let delay = policy.delay(beforeAttempt: attempt + 1, retryAfter: error.retryAfter)
                let delayText = String(format: "%.2f", delay)
                Log.network.info("\(label, privacy: .public) failed (\(error.localizedDescription, privacy: .public)); retry \(attempt + 1) in \(delayText, privacy: .public)s")
                do {
                    try await Task.sleep(for: .seconds(delay))
                } catch {
                    throw APIError.cancelled
                }
                attempt += 1
            }
        }
    }

    private func noteAuthState(failed: Bool) {
        guard reportedAuthFailure != failed else { return }
        reportedAuthFailure = failed
        authStateHandler?(failed)
    }

    private func decode<T: Decodable>(_ type: T.Type, _ data: Data) throws -> T {
        do {
            return try decoder.decode(T.self, from: data)
        } catch {
            throw APIError.decoding(Self.describe(decodingError: error))
        }
    }

    nonisolated static func describe(decodingError error: Error) -> String {
        guard let decodingError = error as? DecodingError else {
            return error.localizedDescription
        }
        func path(_ context: DecodingError.Context) -> String {
            context.codingPath.map(\.stringValue).joined(separator: ".")
        }
        switch decodingError {
        case .keyNotFound(let key, let context):
            return "missing \(key.stringValue) at \(path(context))"
        case .typeMismatch(_, let context), .valueNotFound(_, let context), .dataCorrupted(let context):
            return "\(context.debugDescription) at \(path(context))"
        @unknown default:
            return decodingError.localizedDescription
        }
    }
}
