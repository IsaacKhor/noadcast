//
//  APIDecodingTests.swift
//  NoadcastTests
//
//  Wire decoding against docs/API.md: golden pages, timestamp variants, and
//  the leniency rules (unknown fields/enums tolerated, cursor strict).
//

import Testing
import Foundation
@testable import Noadcast

struct APIDecodingTests {

    /// A complete `/sync` page shaped exactly like docs/API.md.
    private static let goldenSyncPage = #"""
    {
      "instanceId": "0bc1f00d",
      "podcasts": [
        {
          "id": 3,
          "feedUrl": "https://example.com/feed.xml",
          "title": "This American Life",
          "author": "This American Life",
          "summary": "Weekly stories on a theme.",
          "artworkUrl": "https://example.com/art.jpg",
          "language": "en",
          "link": "https://example.com",
          "autoProcessEnabled": true,
          "adAnalysisEnabled": false,
          "episodeCount": 812,
          "latestEpisodeAt": "2026-09-13T20:00:00.000Z",
          "lastFetchAt": "2026-09-22T18:00:00.000Z",
          "lastFetchError": null,
          "createdAt": "2026-09-01T00:00:00.000Z",
          "updatedAt": "2026-09-22T18:03:11.123Z",
          "seq": 1204
        }
      ],
      "episodes": [
        {
          "id": 42,
          "podcastId": 3,
          "guid": "tal-646",
          "title": "646: The Secret of My Death",
          "description": "<p>Show notes</p>",
          "publishedAt": "2026-09-13T20:00:00.000Z",
          "durationSeconds": 3918.94,
          "durationIsMeasured": true,
          "enclosureUrl": "https://example.com/default.mp3",
          "enclosureType": "audio/mpeg",
          "artworkUrl": null,
          "audioState": "present",
          "audioBytes": 63346363,
          "audioSha256": "b9489445",
          "audioContentType": "audio/mpeg",
          "state": "ready",
          "error": null,
          "transcriptState": "ready",
          "classifyState": "ready",
          "markerRevision": 2,
          "adMarkers": [
            {"id": 9, "startSeconds": 0.0, "endSeconds": 78.4, "kind": "intro",
             "summary": "Theme music and preroll", "source": "auto"},
            {"id": 10, "startSeconds": 1210.5, "endSeconds": 1290.0, "kind": "ad",
             "summary": "Sponsor read", "source": "manual"}
          ],
          "updatedAt": "2026-09-22T18:03:11.123Z",
          "seq": 9917
        }
      ],
      "deletions": [{"entity": "podcast", "id": 7}, {"entity": "episode", "id": 99}],
      "settings": {
        "adAnalysisEnabled": true,
        "autoProcessEnabled": false,
        "classifier": "gemini",
        "classifierModel": "gemini-3.5-flash",
        "availableClassifiers": {"gemini": false, "claude": false, "gemini-audio": false, "fake": true}
      },
      "nextSince": 9917,
      "hasMore": false,
      "serverTime": "2026-09-22T18:03:11.123Z"
    }
    """#

    private func decodePage(_ json: String) throws -> SyncPageDTO {
        try APIJSON.makeDecoder().decode(SyncPageDTO.self, from: Data(json.utf8))
    }

    @Test func fullSyncPageDecodesFromGoldenJSON() throws {
        let page = try decodePage(Self.goldenSyncPage)

        #expect(page.instanceId == "0bc1f00d")
        #expect(page.nextSince == 9917)
        #expect(!page.hasMore)
        let serverTime = try #require(page.serverTime)
        #expect(abs(serverTime.timeIntervalSince1970 - 1_790_100_191.123) < 1e-6)

        #expect(page.podcasts.count == 1)
        let podcast = try #require(page.podcasts.first)
        #expect(podcast.id == 3)
        #expect(podcast.feedUrl == "https://example.com/feed.xml")
        #expect(podcast.title == "This American Life")
        #expect(podcast.author == "This American Life")
        #expect(podcast.artworkUrl == "https://example.com/art.jpg")
        #expect(podcast.language == "en")
        #expect(podcast.autoProcessEnabled)
        #expect(!podcast.adAnalysisEnabled)
        #expect(podcast.episodeCount == 812)
        #expect(podcast.lastFetchError == nil)
        #expect(podcast.seq == 1204)
        let latestEpisodeAt = try #require(podcast.latestEpisodeAt)
        #expect(abs(latestEpisodeAt.timeIntervalSince1970 - 1_789_329_600.0) < 1e-6)
        let lastFetchAt = try #require(podcast.lastFetchAt)
        #expect(abs(lastFetchAt.timeIntervalSince1970 - 1_790_100_000.0) < 1e-6)

        #expect(page.episodes.count == 1)
        let episode = try #require(page.episodes.first)
        #expect(episode.id == 42)
        #expect(episode.podcastId == 3)
        #expect(episode.guid == "tal-646")
        #expect(episode.title == "646: The Secret of My Death")
        #expect(episode.description == "<p>Show notes</p>")
        #expect(episode.durationSeconds == 3918.94)
        #expect(episode.durationIsMeasured)
        #expect(episode.enclosureUrl == "https://example.com/default.mp3")
        #expect(episode.enclosureType == "audio/mpeg")
        #expect(episode.artworkUrl == nil)
        #expect(episode.audioState == .present)
        #expect(episode.audioBytes == 63_346_363)
        #expect(episode.audioSha256 == "b9489445")
        #expect(episode.audioContentType == "audio/mpeg")
        #expect(episode.state == .ready)
        #expect(episode.error == nil)
        #expect(episode.transcriptState == .ready)
        #expect(episode.classifyState == .ready)
        #expect(episode.markerRevision == 2)
        #expect(episode.seq == 9917)
        let publishedAt = try #require(episode.publishedAt)
        #expect(abs(publishedAt.timeIntervalSince1970 - 1_789_329_600.0) < 1e-6)
        #expect(episode.adMarkers == [
            AdMarkerDTO(id: 9, startSeconds: 0.0, endSeconds: 78.4, kind: "intro", summary: "Theme music and preroll", source: "auto"),
            AdMarkerDTO(id: 10, startSeconds: 1_210.5, endSeconds: 1_290.0, kind: "ad", summary: "Sponsor read", source: "manual"),
        ])
        #expect(episode.adMarkers.map(\.segmentKind) == [SegmentKind.intro, SegmentKind.ad])
        #expect(episode.adMarkers.map(\.isManual) == [false, true])

        #expect(page.deletions == [
            DeletionDTO(entity: "podcast", id: 7),
            DeletionDTO(entity: "episode", id: 99),
        ])

        let settings = try #require(page.settings)
        #expect(settings.adAnalysisEnabled)
        #expect(!settings.autoProcessEnabled)
        #expect(settings.classifier == "gemini")
        #expect(settings.classifierModel == "gemini-3.5-flash")
        #expect(settings.availableClassifiers == ["gemini": false, "claude": false, "gemini-audio": false, "fake": true])
    }

    @Test func timestampsAcceptFractionAndOffsetVariants() throws {
        let cases: [(text: String, epoch: Double)] = [
            ("2026-09-22T18:03:11.123Z", 1_790_100_191.123),
            ("2026-09-22T18:03:11Z", 1_790_100_191.0),
            ("2026-09-22T18:03:11.123456+00:00", 1_790_100_191.123456),
            ("2026-09-22T20:03:11.5+02:00", 1_790_100_191.5),
            ("2026-09-13T20:00:00.000Z", 1_789_329_600.0),
            ("2026-09-22T14:03:11.123-0400", 1_790_100_191.123),
            ("2026-09-22 18:03:11,5Z", 1_790_100_191.5),
            ("2026-09-22", 1_790_035_200.0),
        ]
        for entry in cases {
            let parsed = try #require(APIDateParser.parse(entry.text), "\(entry.text)")
            #expect(abs(parsed.timeIntervalSince1970 - entry.epoch) < 1e-6, "\(entry.text)")
        }
    }

    @Test func malformedTimestampsAreRejected() throws {
        let garbage = [
            "garbage",
            "",
            "1790100191",
            "2026/09/22",
            "2026-13-01T00:00:00Z",
            "2026-09-22T25:00:00Z",
            "2026-09-22T18:03:11.Z",
            "2026-09-22T18:03:11Zjunk",
            "2026-09-22T18:03:11+",
        ]
        for text in garbage {
            #expect(APIDateParser.parse(text) == nil, "\(text)")
        }

        // In an optional field a bad timestamp decodes to nil rather than
        // failing the row; a numeric epoch is accepted.
        let json = #"{"id": 1, "feedUrl": "https://example.com/f.xml", "title": "T", "latestEpisodeAt": "last Tuesday", "lastFetchAt": 1790100000}"#
        let podcast = try APIJSON.makeDecoder().decode(PodcastDTO.self, from: Data(json.utf8))
        #expect(podcast.latestEpisodeAt == nil)
        #expect(podcast.lastFetchAt?.timeIntervalSince1970 == 1_790_100_000)
    }

    @Test func unknownEnumStringsDecodeToSafeDefaults() throws {
        let json = #"""
        {
          "nextSince": 12,
          "episodes": [
            {"id": 8, "podcastId": 3, "guid": "g-8", "title": "From a newer server",
             "state": "quantum_processing", "audioState": "teleported",
             "transcriptState": "hallucinated", "classifyState": "vibes",
             "markerRevision": 4,
             "adMarkers": [{"startSeconds": 30.0, "endSeconds": 90.0, "kind": "sponsorship",
                            "summary": "Mattress", "source": "auto"}]}
          ]
        }
        """#
        let page = try decodePage(json)
        let episode = try #require(page.episodes.first)

        #expect(episode.state == .unknown)
        #expect(episode.audioState == .unknown)
        #expect(episode.transcriptState == .unknown)
        #expect(episode.classifyState == .unknown)
        #expect(!episode.state.isActive)
        #expect(!episode.audioState.isPresent)
        #expect(episode.markerRevision == 4)
        let marker = try #require(episode.adMarkers.first)
        #expect(marker.kind == "sponsorship")
        #expect(marker.segmentKind == .ad)
        #expect(!marker.isManual)

        let jobsJSON = #"{"items": [{"episodeId": 42, "state": "transcribing", "stage": "transcribe", "current": 1200.0, "total": 3918.9, "statusText": "Transcribing 20:00 of 65:19"}, {"episodeId": 43, "state": "mastering", "stage": "mastering"}]}"#
        let jobs = try APIJSON.makeDecoder().decode(ActiveJobsDTO.self, from: Data(jobsJSON.utf8))
        #expect(jobs.items.map(\.episodeId) == [42, 43])
        #expect(jobs.items[0].state == .transcribing)
        #expect(jobs.items[0].stage == .transcribe)
        #expect(jobs.items[1].state == .unknown)
        #expect(jobs.items[1].stage == .unknown)
        #expect(jobs.items[1].fraction == nil)
    }

    @Test func missingOptionalAndUnknownExtraFieldsAreTolerated() throws {
        let json = #"""
        {
          "nextSince": 5,
          "futureTopLevel": {"nested": [1, 2, 3]},
          "podcasts": [{"id": 5, "feedUrl": "https://example.com/min.xml", "title": "Minimal",
                        "episodeCount": "lots", "brandNewField": true}],
          "episodes": [{"id": 7, "podcastId": 5, "durationSeconds": "long",
                        "adMarkers": [{"startSeconds": 1, "endSeconds": 2, "confidence": 0.9}],
                        "someFutureObject": {"a": 1}}]
        }
        """#
        let page = try decodePage(json)

        #expect(page.nextSince == 5)
        #expect(page.instanceId == nil)
        #expect(page.deletions.isEmpty)
        #expect(page.settings == nil)
        #expect(!page.hasMore)
        #expect(page.serverTime == nil)

        let podcast = try #require(page.podcasts.first)
        #expect(podcast.id == 5)
        #expect(podcast.title == "Minimal")
        #expect(podcast.episodeCount == 0)
        #expect(podcast.autoProcessEnabled)
        #expect(podcast.adAnalysisEnabled)
        #expect(podcast.author == nil)
        #expect(podcast.latestEpisodeAt == nil)
        #expect(podcast.seq == nil)

        let episode = try #require(page.episodes.first)
        #expect(episode.id == 7)
        #expect(episode.guid == "")
        #expect(episode.title == "Untitled")
        #expect(episode.durationSeconds == nil)
        #expect(!episode.durationIsMeasured)
        #expect(episode.audioState == .absent)
        #expect(episode.state == .discovered)
        #expect(episode.transcriptState == .notStarted)
        #expect(episode.classifyState == .notStarted)
        #expect(episode.markerRevision == 0)
        #expect(episode.adMarkers == [
            AdMarkerDTO(startSeconds: 1, endSeconds: 2, kind: "ad", summary: nil, source: nil),
        ])
    }

    @Test func pagesMissingTheCursorOrMarkerBoundsFailToDecode() throws {
        let missingCursor = #"{"instanceId": "x", "podcasts": [], "episodes": [], "deletions": [], "settings": null, "hasMore": false}"#
        do {
            _ = try decodePage(missingCursor)
            Issue.record("A page without nextSince must not decode")
        } catch DecodingError.keyNotFound(let key, _) {
            #expect(key.stringValue == "nextSince")
        }

        // Markers replace the local set wholesale, so a malformed marker must
        // fail the page instead of silently decoding to "no markers".
        let brokenMarker = #"{"nextSince": 3, "episodes": [{"id": 1, "podcastId": 1, "adMarkers": [{"endSeconds": 5.0}]}]}"#
        do {
            _ = try decodePage(brokenMarker)
            Issue.record("A marker without startSeconds must fail the page")
        } catch is DecodingError {
            // Expected.
        }
    }

}
