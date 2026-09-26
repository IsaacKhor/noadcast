//
//  TestSupport.swift
//  NoadcastTests
//
//  Shared helpers: an in-memory SwiftData store, sync-page fixtures, and a
//  URLProtocol stub standing in for the Noadcast server.
//

import Testing
import Foundation
import SwiftData
@testable import Noadcast

// MARK: - SwiftData

/// A fresh in-memory store with the app's full schema. The app's models are
/// MainActor-isolated (default actor isolation), so every helper that touches
/// them is too.
@MainActor
func makeTestContainer() throws -> ModelContainer {
    let schema = Schema([
        Podcast.self,
        Episode.self,
        AdMarker.self,
        QueueItem.self,
        AppSettings.self,
        UsageHistoryDay.self,
        SyncCursor.self,
    ])
    let configuration = ModelConfiguration(
        UUID().uuidString,
        schema: schema,
        isStoredInMemoryOnly: true,
        cloudKitDatabase: .none
    )
    return try ModelContainer(for: schema, configurations: [configuration])
}

/// The episode row with this `serverID`. Keep `context` alive while the
/// returned model is in use.
@MainActor
func storedEpisode(_ serverID: Int, in context: ModelContext) throws -> Episode? {
    var descriptor = FetchDescriptor<Episode>(
        predicate: #Predicate<Episode> { $0.serverID == serverID }
    )
    descriptor.fetchLimit = 1
    return try context.fetch(descriptor).first
}

/// The podcast row with this `serverID`. Keep `context` alive while the
/// returned model is in use.
@MainActor
func storedPodcast(_ serverID: Int, in context: ModelContext) throws -> Podcast? {
    var descriptor = FetchDescriptor<Podcast>(
        predicate: #Predicate<Podcast> { $0.serverID == serverID }
    )
    descriptor.fetchLimit = 1
    return try context.fetch(descriptor).first
}

/// An episode's marker rows as comparable values, sorted the way
/// `SyncEngine` sorts the server's set.
@MainActor
func markerSignatures(of episode: Episode) -> [MarkerSignature] {
    episode.adMarkers
        .map { marker in
            MarkerSignature(
                startSeconds: marker.startSeconds,
                endSeconds: marker.endSeconds,
                kindRaw: marker.kindRaw,
                summary: marker.summary,
                manual: marker.manuallyEdited
            )
        }
        .sorted()
}

/// One episode's marker state, read back through a fresh context and copied
/// out as plain values (the context is gone when this is returned).
struct StoredMarkerState {
    var signatures: [MarkerSignature]
    var activeAdMarkerCount: Int
    var markerRevision: Int
    var rowIDs: Set<PersistentIdentifier>
}

@MainActor
func storedMarkerState(ofEpisode serverID: Int, in container: ModelContainer) throws -> StoredMarkerState? {
    let context = ModelContext(container)
    guard let episode = try storedEpisode(serverID, in: context) else { return nil }
    return StoredMarkerState(
        signatures: markerSignatures(of: episode),
        activeAdMarkerCount: episode.activeAdMarkerCount,
        markerRevision: episode.markerRevision,
        rowIDs: Set(episode.adMarkers.map { $0.persistentModelID })
    )
}

// MARK: - Sync fixtures

/// DTO builders for `SyncEngine` tests. Every apply uses the same fixed
/// `now`, so nothing depends on the wall clock.
enum SyncFixtures {
    /// 2026-09-22T18:03:11Z
    static let now = Date(timeIntervalSince1970: 1_790_100_191)
    static let instanceId = "test-instance"

    static var options: SyncApplyOptions {
        SyncApplyOptions(allowAutoQueue: false, overrides: .empty, now: now)
    }

    static func podcast(_ id: Int, feedURL: String? = nil, title: String? = nil) -> PodcastDTO {
        PodcastDTO(
            id: id,
            feedUrl: feedURL ?? "https://feeds.example.com/podcast-\(id).xml",
            title: title ?? "Podcast \(id)",
            author: "Author \(id)",
            episodeCount: 2,
            latestEpisodeAt: Date(timeIntervalSince1970: 1_789_329_600),
            lastFetchAt: Date(timeIntervalSince1970: 1_790_100_000)
        )
    }

    static func episode(
        _ id: Int,
        podcast podcastID: Int,
        guid: String? = nil,
        title: String? = nil,
        state: ServerEpisodeState = .ready,
        audioState: ServerAudioState = .present,
        markerRevision: Int = 0,
        markers: [AdMarkerDTO] = []
    ) -> EpisodeDTO {
        EpisodeDTO(
            id: id,
            podcastId: podcastID,
            guid: guid ?? "guid-\(id)",
            title: title ?? "Episode \(id)",
            description: "<p>Show notes for episode \(id)</p>",
            publishedAt: Date(timeIntervalSince1970: 1_789_329_600),
            durationSeconds: 1_800,
            durationIsMeasured: true,
            enclosureUrl: "https://cdn.example.com/episodes/\(id).mp3",
            enclosureType: "audio/mpeg",
            audioState: audioState,
            audioBytes: 28_800_000,
            audioSha256: "sha256-\(id)",
            audioContentType: "audio/mpeg",
            state: state,
            transcriptState: .ready,
            classifyState: .ready,
            markerRevision: markerRevision,
            adMarkers: markers
        )
    }

    static func marker(
        _ start: Double,
        _ end: Double,
        _ kind: SegmentKind = .ad,
        summary: String = "Sponsor",
        manual: Bool = false
    ) -> AdMarkerDTO {
        AdMarkerDTO(
            startSeconds: start,
            endSeconds: end,
            kind: kind.rawValue,
            summary: summary,
            source: manual ? "manual" : "auto"
        )
    }

    static func page(
        podcasts: [PodcastDTO] = [],
        episodes: [EpisodeDTO] = [],
        deletions: [DeletionDTO] = [],
        settings: ServerSettingsDTO? = nil,
        nextSince: Int,
        hasMore: Bool = false,
        instanceId: String = SyncFixtures.instanceId
    ) -> SyncPageDTO {
        SyncPageDTO(
            instanceId: instanceId,
            podcasts: podcasts,
            episodes: episodes,
            deletions: deletions,
            settings: settings,
            nextSince: nextSince,
            hasMore: hasMore
        )
    }
}

// MARK: - Wire JSON for the stub server

/// JSON bodies shaped like docs/API.md, for tests that go through
/// `NoadcastAPIClient` (whose DTOs are decode-only).
enum StubJSON {
    static func podcast(id: Int, feedUrl: String, title: String) -> String {
        #"{"id": \#(id), "feedUrl": "\#(feedUrl)", "title": "\#(title)", "author": "Stub", "autoProcessEnabled": true, "adAnalysisEnabled": true, "episodeCount": 1, "latestEpisodeAt": "2026-09-13T20:00:00.000Z", "lastFetchAt": "2026-09-22T18:00:00.000Z", "lastFetchError": null, "seq": \#(id)}"#
    }

    static func episode(id: Int, podcastId: Int, guid: String, title: String) -> String {
        #"{"id": \#(id), "podcastId": \#(podcastId), "guid": "\#(guid)", "title": "\#(title)", "publishedAt": "2026-09-13T20:00:00.000Z", "durationSeconds": 1800.0, "durationIsMeasured": true, "enclosureUrl": "https://cdn.example.com/episodes/\#(id).mp3", "enclosureType": "audio/mpeg", "audioState": "present", "audioBytes": 28800000, "audioSha256": "sha256-\#(id)", "audioContentType": "audio/mpeg", "state": "ready", "error": null, "transcriptState": "ready", "classifyState": "ready", "markerRevision": 1, "adMarkers": [{"id": \#(id), "startSeconds": 10.0, "endSeconds": 70.0, "kind": "ad", "summary": "Sponsor", "source": "auto"}], "seq": \#(id)}"#
    }

    static func syncPage(
        instanceId: String,
        podcasts: [String] = [],
        episodes: [String] = [],
        nextSince: Int,
        hasMore: Bool
    ) -> String {
        let podcastList = podcasts.joined(separator: ", ")
        let episodeList = episodes.joined(separator: ", ")
        return #"{"instanceId": "\#(instanceId)", "podcasts": [\#(podcastList)], "episodes": [\#(episodeList)], "deletions": [], "settings": null, "nextSince": \#(nextSince), "hasMore": \#(hasMore), "serverTime": "2026-09-22T18:03:11.123Z"}"#
    }

    static func error(code: String, message: String) -> String {
        #"{"error": {"code": "\#(code)", "message": "\#(message)"}}"#
    }
}

// MARK: - URLProtocol stub

/// One canned HTTP response.
struct StubResponse: Sendable {
    var status: Int
    var headers: [String: String]
    var body: Data

    static func json(_ text: String, status: Int = 200, headers: [String: String] = [:]) -> StubResponse {
        var merged = ["Content-Type": "application/json"]
        merged.merge(headers) { _, new in new }
        return StubResponse(status: status, headers: merged, body: Data(text.utf8))
    }

    static var notFound: StubResponse {
        .json(StubJSON.error(code: "notFound", message: "No stub for this request"), status: 404)
    }
}

/// Registry of per-host handlers plus a per-host request log. Lock-protected:
/// `URLProtocol` callbacks run on URLSession's own threads. Tests register a
/// unique host each (see `uniqueHost()`), so parallel tests never share a
/// handler or a log.
final class StubServer: @unchecked Sendable {
    /// `index` is the number of requests this host had already received.
    typealias Handler = @Sendable (_ request: URLRequest, _ index: Int) -> StubResponse

    static let shared = StubServer()

    private let lock = NSLock()
    private var handlers: [String: Handler] = [:]
    private var log: [String: [URLRequest]] = [:]

    static func uniqueHost() -> String {
        "t-\(UUID().uuidString.prefix(8).lowercased()).invalid"
    }

    /// Replaces any handler already registered for `host` (the request log
    /// is kept).
    func register(host: String, handler: @escaping Handler) {
        lock.lock()
        defer { lock.unlock() }
        handlers[host.lowercased()] = handler
    }

    func unregister(host: String) {
        lock.lock()
        defer { lock.unlock() }
        handlers[host.lowercased()] = nil
        log[host.lowercased()] = nil
    }

    /// Every request `host` received, in arrival order.
    func requests(for host: String) -> [URLRequest] {
        lock.lock()
        defer { lock.unlock() }
        return log[host.lowercased()] ?? []
    }

    fileprivate func respond(to request: URLRequest) -> StubResponse {
        let host = request.url?.host?.lowercased() ?? ""
        lock.lock()
        let index = log[host]?.count ?? 0
        log[host, default: []].append(request)
        let handler = handlers[host]
        lock.unlock()
        // Called outside the lock so a handler may use the registry.
        return handler?(request, index) ?? .notFound
    }

    static func queryValue(_ name: String, in request: URLRequest) -> String? {
        guard let url = request.url,
              let components = URLComponents(url: url, resolvingAgainstBaseURL: false)
        else { return nil }
        return components.queryItems?.first(where: { $0.name == name })?.value
    }
}

/// Answers every request from `StubServer.shared`, keyed by the request's host.
final class StubURLProtocol: URLProtocol {
    override class func canInit(with request: URLRequest) -> Bool {
        true
    }

    override class func canonicalRequest(for request: URLRequest) -> URLRequest {
        request
    }

    override func startLoading() {
        guard let url = request.url else {
            client?.urlProtocol(self, didFailWithError: URLError(.badURL))
            return
        }
        let stub = StubServer.shared.respond(to: request)
        guard let response = HTTPURLResponse(
            url: url,
            statusCode: stub.status,
            httpVersion: "HTTP/1.1",
            headerFields: stub.headers
        ) else {
            client?.urlProtocol(self, didFailWithError: URLError(.badServerResponse))
            return
        }
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        if !stub.body.isEmpty {
            client?.urlProtocol(self, didLoad: stub.body)
        }
        client?.urlProtocolDidFinishLoading(self)
    }

    override func stopLoading() {}

    static func makeSession() -> URLSession {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [StubURLProtocol.self]
        configuration.urlCache = nil
        configuration.requestCachePolicy = .reloadIgnoringLocalCacheData
        return URLSession(configuration: configuration)
    }
}

/// A `NoadcastAPIClient` pointed at `http://<host>` on the stub session.
func makeStubClient(
    host: String,
    retryPolicy: RetryPolicy = RetryPolicy(maxAttempts: 1, baseDelay: 0, maxDelay: 0, maxRetryAfter: 0),
    authStateHandler: (@Sendable (Bool) -> Void)? = nil
) throws -> NoadcastAPIClient {
    guard let baseURL = URL(string: "http://\(host)") else { throw URLError(.badURL) }
    let endpoint = APIConfiguration.Endpoint(baseURL: baseURL, token: "test-token")
    return NoadcastAPIClient(
        session: StubURLProtocol.makeSession(),
        endpointProvider: { endpoint },
        retryPolicy: retryPolicy,
        authStateHandler: authStateHandler
    )
}

/// A lock-protected value for `@Sendable` callbacks.
final class LockedBox<Value>: @unchecked Sendable {
    private let lock = NSLock()
    private var storage: Value

    init(_ value: Value) {
        storage = value
    }

    var value: Value {
        lock.lock()
        defer { lock.unlock() }
        return storage
    }

    func withLock<T>(_ body: (inout Value) -> T) -> T {
        lock.lock()
        defer { lock.unlock() }
        return body(&storage)
    }
}

// MARK: - SyncService harness

/// A `SyncService` + `DeviceStateRestoreService` pair on an in-memory store,
/// talking to the stub server, with every app-wide side effect switched off.
/// Snapshots the `UserDefaults` keys the sync path writes and puts them back
/// in `cleanUp()`.
@MainActor
final class SyncHarness {
    let container: ModelContainer
    let client: NoadcastAPIClient
    let restore: DeviceStateRestoreService
    let service: SyncService
    let jobsDirectory: URL

    private let previousNeedsFullResync: Bool
    private let previousRestoreStatus: Any?

    init(host: String) throws {
        let container = try makeTestContainer()
        let client = try makeStubClient(host: host)
        let jobsDirectory = FileManager.default.temporaryDirectory
            .appendingPathComponent("NoadcastTests-restore-\(UUID().uuidString)", isDirectory: true)
        let restore = DeviceStateRestoreService(
            api: client,
            jobsDirectory: jobsDirectory,
            performsSideEffects: false,
            isConfigured: { true }
        )
        restore.configure(container: container)
        let service = SyncService(
            api: client,
            restoreService: restore,
            performsSideEffects: false,
            isConfigured: { true }
        )
        service.configure(container: container)

        self.container = container
        self.client = client
        self.restore = restore
        self.service = service
        self.jobsDirectory = jobsDirectory
        previousNeedsFullResync = SyncFlags.needsFullResync
        previousRestoreStatus = UserDefaults.standard.object(forKey: DeviceStateRestoreService.statusDefaultsKey)
        // A leftover flag from the host app would turn a delta sync into a
        // full resync with sweep.
        SyncFlags.needsFullResync = false
    }

    func cleanUp() {
        SyncFlags.needsFullResync = previousNeedsFullResync
        if let previousRestoreStatus {
            UserDefaults.standard.set(previousRestoreStatus, forKey: DeviceStateRestoreService.statusDefaultsKey)
        } else {
            UserDefaults.standard.removeObject(forKey: DeviceStateRestoreService.statusDefaultsKey)
        }
        try? FileManager.default.removeItem(at: jobsDirectory)
    }
}
