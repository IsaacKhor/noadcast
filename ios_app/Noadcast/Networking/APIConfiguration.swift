import Foundation

/// Where the Noadcast server lives and how to authenticate against it.
///
/// * The base URL is kept in `UserDefaults`: it must be readable before the
///   `ModelContainer` opens (the launch-time migration consults it) and from
///   any thread (the download delegate builds requests off-main).
/// * The bearer token lives in the Keychain (`KeychainStore`), readable
///   after first unlock so background refresh and background downloads work
///   while the device is locked.
///
/// The server is expected to be reached over Tailscale (plain HTTP over the
/// tailnet; `Info.plist` sets `NSAllowsArbitraryLoads`).
nonisolated enum APIConfiguration {
    /// Shown as the placeholder in the setup UI.
    static let placeholderBaseURL = "http://laurel.turkey-galaxy.ts.net:44007"

    static let baseURLDefaultsKey = "ServerBaseURL"
    static let tokenKeychainAccount = "api-token"

    /// Resolved server address + credential for one request.
    nonisolated struct Endpoint: Sendable, Equatable {
        let baseURL: URL
        let token: String?

        /// `base + path`, preserving any path prefix on the base URL (e.g. a
        /// reverse proxy mounted at `/noadcast`).
        func url(path: String, query: [URLQueryItem] = []) -> URL? {
            guard var components = URLComponents(url: baseURL, resolvingAgainstBaseURL: false) else {
                return nil
            }
            var basePath = components.path
            while basePath.hasSuffix("/") { basePath.removeLast() }
            let suffix = path.hasPrefix("/") ? path : "/" + path
            components.path = basePath + suffix
            components.queryItems = query.isEmpty ? nil : query
            return components.url
        }

        /// Resolves a server-issued path that may carry its own query string
        /// (e.g. the signed audio path `/api/v1/episodes/42/audio?exp=…&sig=…`)
        /// against the configured base URL.
        func resolve(pathAndQuery: String) -> URL? {
            var base = baseURL.absoluteString
            while base.hasSuffix("/") { base.removeLast() }
            let suffix = pathAndQuery.hasPrefix("/") ? pathAndQuery : "/" + pathAndQuery
            return URL(string: base + suffix)
        }
    }

    // MARK: - Reading

    static var baseURLString: String? {
        UserDefaults.standard.string(forKey: baseURLDefaultsKey)
    }

    static var baseURL: URL? {
        baseURLString.flatMap(normalizedBaseURL(from:))
    }

    /// `nil` or empty means "send no Authorization header" (a server started
    /// without auth). Cached in memory; the Keychain is read once.
    static var token: String? {
        TokenCache.shared.token
    }

    /// The server address is set. Token presence is not required: `/health`
    /// reports `authRequired`, and a 401 surfaces through the auth banner.
    static var isConfigured: Bool {
        baseURL != nil
    }

    static var currentEndpoint: Endpoint? {
        guard let baseURL else { return nil }
        return Endpoint(baseURL: baseURL, token: token)
    }

    // MARK: - Writing

    /// Persists a new server address and token. Returns `false` if the
    /// address is not a usable http(s) URL.
    @discardableResult
    static func save(baseURLString: String, token: String?) -> Bool {
        guard let url = normalizedBaseURL(from: baseURLString) else { return false }
        UserDefaults.standard.set(url.absoluteString, forKey: baseURLDefaultsKey)
        let trimmed = token?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        TokenCache.shared.set(trimmed.isEmpty ? nil : trimmed)
        return true
    }

    /// Forgets the token (and optionally the address). Local data is kept so
    /// downloaded episodes stay playable.
    static func signOut(forgetServerAddress: Bool) {
        TokenCache.shared.set(nil)
        if forgetServerAddress {
            UserDefaults.standard.removeObject(forKey: baseURLDefaultsKey)
        }
    }

    // MARK: - Normalisation

    /// Accepts `host:port`, `http://host:port/`, or a pasted `…/api/v1` URL
    /// and returns the canonical base (`http://host:port`), or `nil` if the
    /// string is not a usable http(s) address.
    static func normalizedBaseURL(from string: String) -> URL? {
        var text = string.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return nil }
        if !text.contains("://") {
            text = "http://" + text
        }
        guard var components = URLComponents(string: text),
              let scheme = components.scheme?.lowercased(),
              scheme == "http" || scheme == "https",
              let host = components.host, !host.isEmpty
        else { return nil }
        components.scheme = scheme
        components.host = host.lowercased()
        components.query = nil
        components.fragment = nil
        var path = components.path
        while path.hasSuffix("/") { path.removeLast() }
        if path.hasSuffix("/api/v1") {
            path.removeLast("/api/v1".count)
        }
        while path.hasSuffix("/") { path.removeLast() }
        components.path = path
        return components.url
    }
}

/// Keychain-backed token with an in-memory cache, safe to read from any
/// thread (row bodies, the download delegate, the API actor).
///
/// Only a successful read is cached: a miss can mean "locked before first
/// unlock" during a background launch after reboot, and caching that would
/// hide the token until the next cold start.
nonisolated final class TokenCache: @unchecked Sendable {
    static let shared = TokenCache()

    private let lock = NSLock()
    private var hasCachedValue = false
    private var cached: String?

    var token: String? {
        lock.lock()
        defer { lock.unlock() }
        if hasCachedValue {
            return cached
        }
        let value = KeychainStore.read(account: APIConfiguration.tokenKeychainAccount)
        if value != nil {
            cached = value
            hasCachedValue = true
        }
        return value
    }

    func set(_ value: String?) {
        lock.lock()
        defer { lock.unlock() }
        if let value {
            KeychainStore.write(value, account: APIConfiguration.tokenKeychainAccount)
        } else {
            KeychainStore.delete(account: APIConfiguration.tokenKeychainAccount)
        }
        cached = value
        hasCachedValue = true
    }
}
