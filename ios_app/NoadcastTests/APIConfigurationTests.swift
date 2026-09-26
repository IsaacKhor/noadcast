//
//  APIConfigurationTests.swift
//  NoadcastTests
//
//  Server-address normalisation and URL building against the configured base.
//

import Testing
import Foundation
@testable import Noadcast

struct APIConfigurationTests {

    @Test func normalizedBaseURLAcceptsHostPortAndPastedURLs() {
        func normalized(_ text: String) -> String? {
            APIConfiguration.normalizedBaseURL(from: text)?.absoluteString
        }

        #expect(normalized("laurel.turkey-galaxy.ts.net:8765") == "http://laurel.turkey-galaxy.ts.net:8765")
        #expect(normalized("http://laurel.turkey-galaxy.ts.net:8765/") == "http://laurel.turkey-galaxy.ts.net:8765")
        #expect(normalized("http://laurel.turkey-galaxy.ts.net:8765/api/v1") == "http://laurel.turkey-galaxy.ts.net:8765")
        #expect(normalized("http://laurel.turkey-galaxy.ts.net:8765/api/v1/") == "http://laurel.turkey-galaxy.ts.net:8765")
        #expect(normalized("https://Noadcast.Example.com/api/v1") == "https://noadcast.example.com")
        #expect(normalized("  http://10.0.0.5:8765  \n") == "http://10.0.0.5:8765")
        #expect(normalized("http://host.example:8765/?debug=1#top") == "http://host.example:8765")
        // A reverse-proxy prefix survives; only the API suffix is stripped.
        #expect(normalized("http://proxy.local/noadcast/api/v1") == "http://proxy.local/noadcast")
    }

    @Test func normalizedBaseURLRejectsUnusableStrings() {
        #expect(APIConfiguration.normalizedBaseURL(from: "ftp://x") == nil)
        #expect(APIConfiguration.normalizedBaseURL(from: "file:///etc/hosts") == nil)
        #expect(APIConfiguration.normalizedBaseURL(from: "") == nil)
        #expect(APIConfiguration.normalizedBaseURL(from: "   ") == nil)
    }

    @Test func endpointURLKeepsTheBasePathPrefix() throws {
        let proxied = APIConfiguration.Endpoint(
            baseURL: try #require(URL(string: "http://proxy.local/noadcast")),
            token: nil
        )
        let sync = proxied.url(
            path: "/api/v1/sync",
            query: [URLQueryItem(name: "since", value: "0"), URLQueryItem(name: "limit", value: "500")]
        )
        #expect(sync?.absoluteString == "http://proxy.local/noadcast/api/v1/sync?since=0&limit=500")
        #expect(proxied.url(path: "api/v1/session")?.absoluteString == "http://proxy.local/noadcast/api/v1/session")

        let trailingSlash = APIConfiguration.Endpoint(
            baseURL: try #require(URL(string: "http://proxy.local/noadcast/")),
            token: "t"
        )
        #expect(trailingSlash.url(path: "/api/v1/sync")?.absoluteString == "http://proxy.local/noadcast/api/v1/sync")

        let root = APIConfiguration.Endpoint(
            baseURL: try #require(URL(string: "http://laurel.turkey-galaxy.ts.net:8765")),
            token: nil
        )
        #expect(root.url(path: "/health")?.absoluteString == "http://laurel.turkey-galaxy.ts.net:8765/health")
    }

    @Test func endpointResolveKeepsTheSignedQueryIntact() throws {
        let signedPath = "/api/v1/episodes/42/audio?exp=1790000000&sig=9f86d081884c7d659a2feaa0c55ad015"
        let root = APIConfiguration.Endpoint(
            baseURL: try #require(URL(string: "http://laurel.turkey-galaxy.ts.net:8765")),
            token: "secret-token"
        )

        let url = try #require(root.resolve(pathAndQuery: signedPath))

        #expect(url.absoluteString == "http://laurel.turkey-galaxy.ts.net:8765" + signedPath)
        #expect(url.path == "/api/v1/episodes/42/audio")
        #expect(url.query == "exp=1790000000&sig=9f86d081884c7d659a2feaa0c55ad015")
        // The bearer token never leaks into a URL.
        #expect(!url.absoluteString.contains("secret-token"))

        let proxied = APIConfiguration.Endpoint(
            baseURL: try #require(URL(string: "http://proxy.local/noadcast/")),
            token: nil
        )
        #expect(proxied.resolve(pathAndQuery: signedPath)?.absoluteString == "http://proxy.local/noadcast" + signedPath)
    }

}
