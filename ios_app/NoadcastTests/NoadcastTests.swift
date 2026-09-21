//
//  NoadcastTests.swift
//  NoadcastTests
//
//  Created by Isaac Khor on 2026.05.15.
//

import Testing
import Foundation
import SwiftData
@testable import Noadcast

struct NoadcastTests {

    @Test @MainActor func podcastDefaultsToAnalysisEnabled() async throws {
        let podcast = Podcast(
            feedURL: try #require(URL(string: "https://example.com/feed.xml")),
            title: "Example"
        )

        #expect(podcast.autoDownloadEnabled)
        #expect(podcast.aiProcessingEnabled)
    }

    @Test @MainActor func globalAdAnalysisDefaultsOff() async throws {
        let settings = AppSettings()

        #expect(!settings.adAnalysisEnabled)
    }

    @Test @MainActor func adDetectionBackendDefaultsToDirectGemini() async throws {
        let settings = AppSettings()

        #expect(settings.adDetectionBackend == .geminiFiles)
        #expect(AdDetectionBackend.allCases == [.geminiFiles, .openRouter, .whisperServer, .appleSpeech])
        #expect(settings.openRouterAPIKey == nil)
        #expect(settings.adDetectionServerHost == "http://127.0.0.1")
        #expect(settings.adDetectionServerPort == 8765)
    }

    @Test func newestGeminiModelsMapToNativeAndOpenRouterIDs() {
        #expect(AdDetectionProvider.gemini36Flash.apiModel == "gemini-3.6-flash")
        #expect(AdDetectionProvider.gemini36Flash.openRouterAPIModel == "google/gemini-3.6-flash")
        #expect(AdDetectionProvider.gemini37Flash.apiModel == "gemini-3.7-flash")
        #expect(AdDetectionProvider.gemini37Flash.openRouterAPIModel == "google/gemini-3.7-flash")
        #expect(AdDetectionProvider.allCases.contains(.gemini36Flash))
        #expect(AdDetectionProvider.allCases.contains(.gemini37Flash))
    }

    @Test func durationPromptNamesThePhysicalOutroEndpoint() {
        let guidance = CloudAdDetectionService.durationPromptContext(3_723.5)

        #expect(guidance.contains("3723.50 seconds"))
        #expect(guidance.contains("outro"))
        #expect(guidance.contains("endSeconds"))
        #expect(CloudAdDetectionService.durationPromptContext(nil).isEmpty)
        #expect(CloudAdDetectionService.durationPromptContext(.nan).isEmpty)
        #expect(CloudAdDetectionService.durationPromptContext(0).isEmpty)
        #expect(CloudAdDetectionService.transcriptEndpointGuidance(
            episodeDuration: 100,
            transcriptEnd: 90
        ).contains("100.00 seconds"))
        #expect(CloudAdDetectionService.transcriptEndpointGuidance(
            episodeDuration: 80,
            transcriptEnd: 90
        ).contains("endpoint is unavailable"))
        #expect(CloudAdDetectionService.transcriptEndpointGuidance(
            episodeDuration: nil,
            transcriptEnd: 90
        ).contains("final transcript timestamp"))
        #expect(CloudAdDetectionService.segmentsOnlyPrompt.contains("deliberately inspect the final portion"))
        #expect(CloudAdDetectionService.transcriptSegmentsPrompt.contains("physical episode endpoint"))
    }

    @Test func openRouterAudioFormatsAreMappedWithoutRelabelingBytes() {
        #expect(CloudAdDetectionService.openRouterAudioFormat(
            mimeType: "audio/mpeg",
            fileExtension: "mp3"
        ) == "mp3")
        #expect(CloudAdDetectionService.openRouterAudioFormat(
            mimeType: "audio/mp4",
            fileExtension: "m4a"
        ) == "m4a")
        #expect(CloudAdDetectionService.openRouterAudioFormat(
            mimeType: "application/octet-stream",
            fileExtension: "flac"
        ) == "flac")
        #expect(CloudAdDetectionService.openRouterAudioFormat(
            mimeType: "application/octet-stream",
            fileExtension: "bin"
        ) == nil)
    }

    @Test func openRouterBase64StreamingHandlesChunkBoundaries() throws {
        for byteCount in 0...13 {
            let sourceData = Data((0..<byteCount).map { UInt8($0 & 0xff) })
            for chunkSize in 1...5 {
                let token = UUID().uuidString
                let sourceURL = FileManager.default.temporaryDirectory
                    .appendingPathComponent("noadcast-base64-source-\(token)")
                let outputURL = FileManager.default.temporaryDirectory
                    .appendingPathComponent("noadcast-base64-output-\(token)")
                defer {
                    try? FileManager.default.removeItem(at: sourceURL)
                    try? FileManager.default.removeItem(at: outputURL)
                }
                try sourceData.write(to: sourceURL)
                #expect(FileManager.default.createFile(atPath: outputURL.path, contents: nil))
                let output = try FileHandle(forWritingTo: outputURL)
                try CloudAdDetectionService.writeBase64EncodedContents(
                    of: sourceURL,
                    to: output,
                    chunkSize: chunkSize
                )
                try output.close()

                let actual = try Data(contentsOf: outputURL)
                #expect(actual == sourceData.base64EncodedData())
            }
        }
    }

    @Test @MainActor func resetPlaybackHistoryClearsListeningTotals() async throws {
        let settings = AppSettings()
        settings.lifetimePlayedSeconds = 120
        settings.lifetimeAdSkipSeconds = 30

        settings.resetPlaybackHistoryStatistics()

        #expect(settings.lifetimePlayedSeconds == 0)
        #expect(settings.lifetimeAdSkipSeconds == 0)
    }

    @Test func detectedAdSanitizerClampsToEpisodeDuration() async throws {
        let ad = DetectedAd(
            startSeconds: 95,
            endSeconds: 120,
            summary: "Post-roll",
            kind: .outro
        )

        let sanitized = try #require(ad.sanitized(episodeDuration: 100))

        #expect(sanitized.startSeconds == 95)
        #expect(sanitized.endSeconds == 100)
    }

    @Test func detectedOutroExtendsToEpisodeEndButAdDoesNot() throws {
        let outro = DetectedAd(
            startSeconds: 80,
            endSeconds: 90,
            summary: "Farewell",
            kind: .outro
        )
        let ad = DetectedAd(
            startSeconds: 80,
            endSeconds: 90,
            summary: "Sponsor",
            kind: .ad
        )

        #expect(try #require(outro.sanitized(episodeDuration: 100)).endSeconds == 100)
        #expect(try #require(ad.sanitized(episodeDuration: 100)).endSeconds == 90)
    }

    @Test func detectedAdSanitizerDropsSegmentsOutsideEpisode() async throws {
        let ad = DetectedAd(
            startSeconds: 105,
            endSeconds: 120,
            summary: "Impossible marker",
            kind: .ad
        )

        #expect(ad.sanitized(episodeDuration: 100)?.startSeconds == nil)
    }

    @Test func detectedAdSanitizerDropsNonFiniteSegments() async throws {
        let ad = DetectedAd(
            startSeconds: 10,
            endSeconds: .infinity,
            summary: "Impossible marker",
            kind: .ad
        )

        #expect(ad.sanitized(episodeDuration: 100)?.startSeconds == nil)
    }

    @Test func playerSeekClampingAllowsUnknownDuration() async throws {
        #expect(PlayerService.clampedPlaybackTime(42, duration: 0) == 42)
        #expect(PlayerService.clampedPlaybackTime(120, duration: 100) == 100)
        #expect(PlayerService.clampedPlaybackTime(.nan, duration: 100) == 0)
    }

    @Test @MainActor func queueDismissalMarksPlayedAndRemovesQueueItem() throws {
        let schema = Schema([
            Podcast.self,
            Episode.self,
            QueueItem.self,
            AdMarker.self,
        ])
        let configuration = ModelConfiguration(schema: schema, isStoredInMemoryOnly: true)
        let container = try ModelContainer(for: schema, configurations: [configuration])
        let context = container.mainContext
        let episode = Episode(
            guid: "queue-dismissal-test",
            title: "Queue dismissal",
            audioURL: try #require(URL(string: "https://example.com/audio.mp3"))
        )
        context.insert(episode)
        context.insert(QueueItem(position: 0, episode: episode))
        try context.save()

        SubscriptionService.shared.deleteEpisodeContent(
            episode,
            in: context,
            markAsPlayed: true
        )

        #expect(episode.isPlayed)
        #expect(episode.datePlayed != nil)
        #expect(try context.fetchCount(FetchDescriptor<QueueItem>()) == 0)
    }

    @Test @MainActor func transcriptionProgressShowsAudioTime() async throws {
        let episode = Episode(
            guid: "progress-test",
            title: "Progress",
            audioURL: try #require(URL(string: "https://example.com/audio.mp3"))
        )
        episode.processingState = .detectingAds
        episode.processingStatusText = "Transcribing locally with Apple…"
        episode.processingCurrent = 65
        episode.processingTotal = 90

        #expect(TimeFormatting.progressDetail(for: episode) == "1:05 / 1:30")
    }

    @Test func whisperServerURLDefaultsToAnalyzeEndpoint() async throws {
        let url = try CloudAdDetectionService.serverAnalyzeURL(
            host: "127.0.0.1",
            port: 8765
        )

        #expect(url.absoluteString == "http://127.0.0.1:8765/analyze")
    }

    @Test func whisperServerURLPreservesBasePath() async throws {
        let url = try CloudAdDetectionService.serverAnalyzeURL(
            host: "http://example.local/noadcast",
            port: 8080
        )

        #expect(url.absoluteString == "http://example.local:8080/noadcast/analyze")
    }

}
