//
//  DeviceStateSnapshotTests.swift
//  NoadcastTests
//
//  The feedURL + guid snapshot that survives a mirror wipe, and the feed-URL
//  identity it is matched by.
//

import Testing
import Foundation
@testable import Noadcast

struct DeviceStateSnapshotTests {

    @Test func snapshotRoundTripsThroughJSON() throws {
        let feed = "https://feeds.example.com/show.xml"
        let snapshot = DeviceStateSnapshot(
            source: .instanceChange,
            createdAt: Date(timeIntervalSinceReferenceDate: 811_792_991.25),
            podcasts: [
                DeviceStateSnapshot.PodcastState(
                    feedURL: feed,
                    title: "Show",
                    autoDownloadEnabled: false,
                    customPlaybackSpeed: 1.5,
                    adAnalysisEnabled: nil
                ),
                DeviceStateSnapshot.PodcastState(
                    feedURL: "https://other.example.com/rss",
                    title: nil,
                    autoDownloadEnabled: true,
                    customPlaybackSpeed: nil,
                    adAnalysisEnabled: false
                ),
            ],
            episodes: [
                DeviceStateSnapshot.EpisodeState(
                    feedURL: feed,
                    guid: "ep-1",
                    title: "One",
                    localFilename: "srv-10-abcd1234.mp3",
                    fileSizeBytes: 63_346_363,
                    audioMimeType: "audio/mpeg",
                    playbackPosition: 1_234.5,
                    isPlayed: false,
                    datePlayed: nil
                ),
                DeviceStateSnapshot.EpisodeState(
                    feedURL: feed,
                    guid: "ep-2",
                    title: nil,
                    localFilename: nil,
                    fileSizeBytes: nil,
                    audioMimeType: nil,
                    playbackPosition: 0,
                    isPlayed: true,
                    datePlayed: Date(timeIntervalSinceReferenceDate: 811_700_000)
                ),
            ],
            queue: [DeviceStateSnapshot.EpisodeKey(feedURL: feed, guid: "ep-1")],
            lastPlayed: DeviceStateSnapshot.EpisodeKey(feedURL: feed, guid: "ep-1"),
            stats: DeviceStateSnapshot.Stats(lifetimePlayedSeconds: 36_000, lifetimeAdSkipSeconds: 1_800),
            usageDays: [
                DeviceStateSnapshot.UsageDay(
                    dayStart: Date(timeIntervalSinceReferenceDate: 811_728_000),
                    playbackSeconds: 3_600,
                    adSkippedSeconds: 240
                ),
            ],
            preferences: DeviceStateSnapshot.Preferences(
                defaultPlaybackSpeed: 1.25,
                autoDownloadPolicy: AutoDownloadPolicy.wifiOnly.rawValue,
                autoDeleteAfterPlayed: true,
                podcastSortMode: PodcastSortMode.alphabetical.rawValue,
                skipAds: true,
                skipIntrosAndOutros: false,
                chainSkipGapSeconds: 5,
                adAnalysisEnabled: true
            )
        )

        let data = try DeviceStateSnapshot.encode(snapshot)
        let decoded = try DeviceStateSnapshot.decode(data)

        #expect(decoded == snapshot)
        #expect(decoded.version == 1)
        #expect(decoded.source == .instanceChange)
        #expect(!decoded.isEmpty)
        #expect(decoded.episodes.first?.key == DeviceStateSnapshot.EpisodeKey(feedURL: feed, guid: "ep-1"))
        #expect(DeviceStateSnapshot(source: .localReset).isEmpty)
    }

    @Test func feedURLKeyTreatsCosmeticRewritesAsEqual() {
        let canonical = FeedURLKey.normalize("https://example.com/feed.xml")
        #expect(canonical == "example.com/feed.xml")

        let variants = [
            "http://example.com/feed.xml",
            "https://EXAMPLE.com/feed.xml",
            "https://www.example.com/feed.xml",
            "https://example.com:443/feed.xml",
            "http://example.com:80/feed.xml",
            "https://example.com/feed.xml/",
            "  http://www.Example.COM:80/feed.xml//  ",
        ]
        for variant in variants {
            #expect(FeedURLKey.normalize(variant) == canonical, "\(variant)")
        }

        // Real differences stay different: a non-default port, another path,
        // or another query (it can select a different feed).
        #expect(FeedURLKey.normalize("https://example.com:8443/feed.xml") != canonical)
        #expect(FeedURLKey.normalize("https://example.com/other.xml") != canonical)
        #expect(FeedURLKey.normalize("https://example.com/feed.xml?id=1") != FeedURLKey.normalize("https://example.com/feed.xml?id=2"))
        #expect(FeedURLKey.normalize("https://example.com/feed/?id=1") == FeedURLKey.normalize("http://www.example.com/feed?id=1"))
    }

    @Test func episodeMatchKeyUsesTheNormalisedFeed() {
        let original = DeviceStateSnapshot.EpisodeKey(feedURL: "http://www.Example.com/feed/", guid: "tal-646")
        let rewritten = DeviceStateSnapshot.EpisodeKey(feedURL: "https://example.com/feed", guid: "tal-646")
        let otherGUID = DeviceStateSnapshot.EpisodeKey(feedURL: "https://example.com/feed", guid: "TAL-646")

        #expect(original.matchKey == rewritten.matchKey)
        #expect(original.matchKey != otherGUID.matchKey)
        // The stored key keeps the original spelling; only matching normalises.
        #expect(original != rewritten)
    }

}
