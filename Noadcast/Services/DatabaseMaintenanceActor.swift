import Foundation
import SwiftData
import os

nonisolated struct DuplicateCleanupReport: Sendable {
    var podcastsRemoved = 0
    var episodesRemoved = 0
    var queueItemsRemoved = 0
    var transcriptSegmentsRemoved = 0
    var adMarkersRemoved = 0

    var changed: Bool {
        podcastsRemoved > 0
            || episodesRemoved > 0
            || queueItemsRemoved > 0
            || transcriptSegmentsRemoved > 0
            || adMarkersRemoved > 0
    }
}

/// Repairs duplicate rows left by older builds or interrupted write cycles.
///
/// This intentionally runs on a `@ModelActor`: duplicate cleanup walks
/// relationships and may touch local files, so it should not share the main
/// context SwiftUI is scrolling with.
@ModelActor
actor DatabaseMaintenanceActor {
    func cleanupDuplicates() -> DuplicateCleanupReport {
        let state = Log.signposter.beginInterval("DatabaseMaintenance.cleanupDuplicates")
        defer { Log.signposter.endInterval("DatabaseMaintenance.cleanupDuplicates", state) }

        var report = DuplicateCleanupReport()
        report.podcastsRemoved += mergeDuplicatePodcasts()
        report.episodesRemoved += mergeDuplicateEpisodes()
        report.queueItemsRemoved += removeDuplicateQueueItems()
        let childReport = removeDuplicateChildRowsAndRefreshDerivedState()
        report.transcriptSegmentsRemoved += childReport.transcriptSegmentsRemoved
        report.adMarkersRemoved += childReport.adMarkersRemoved

        try? modelContext.save()
        if report.changed {
            Log.startup.notice("Duplicate cleanup removed podcasts=\(report.podcastsRemoved) episodes=\(report.episodesRemoved) queueItems=\(report.queueItemsRemoved) transcriptSegments=\(report.transcriptSegmentsRemoved) adMarkers=\(report.adMarkersRemoved)")
        }
        return report
    }

    private func mergeDuplicatePodcasts() -> Int {
        let podcasts = (try? modelContext.fetch(FetchDescriptor<Podcast>())) ?? []
        let groups = Dictionary(grouping: podcasts) { podcast in
            podcast.feedURL.absoluteString
        }

        var removed = 0
        for duplicates in groups.values where duplicates.count > 1 {
            let canonical = duplicates.max(by: { podcastScore($0) < podcastScore($1) }) ?? duplicates[0]
            for duplicate in duplicates where duplicate !== canonical {
                merge(duplicatePodcast: duplicate, into: canonical)
                modelContext.delete(duplicate)
                removed += 1
            }
            refreshPodcastDerivedState(canonical)
        }
        return removed
    }

    private func mergeDuplicateEpisodes() -> Int {
        let episodes = (try? modelContext.fetch(FetchDescriptor<Episode>())) ?? []
        let groups = Dictionary(grouping: episodes, by: episodeDedupeKey)

        var removed = 0
        for duplicates in groups.values where duplicates.count > 1 {
            let canonical = duplicates.max(by: { episodeScore($0) < episodeScore($1) }) ?? duplicates[0]
            for duplicate in duplicates where duplicate !== canonical {
                merge(duplicateEpisode: duplicate, into: canonical)
                modelContext.delete(duplicate)
                removed += 1
            }
        }
        return removed
    }

    private func removeDuplicateQueueItems() -> Int {
        let descriptor = FetchDescriptor<QueueItem>(
            sortBy: [
                SortDescriptor(\.position),
                SortDescriptor(\.addedAt)
            ]
        )
        let queueItems = (try? modelContext.fetch(descriptor)) ?? []
        var seenEpisodeIDs = Set<PersistentIdentifier>()
        var kept: [QueueItem] = []
        var removed = 0

        for item in queueItems {
            guard let episode = item.episode else {
                modelContext.delete(item)
                removed += 1
                continue
            }
            let episodeID = episode.persistentModelID
            guard seenEpisodeIDs.insert(episodeID).inserted else {
                modelContext.delete(item)
                removed += 1
                continue
            }
            kept.append(item)
        }

        for (index, item) in kept.enumerated() where item.position != index {
            item.position = index
        }
        return removed
    }

    private func removeDuplicateChildRowsAndRefreshDerivedState() -> DuplicateCleanupReport {
        var report = DuplicateCleanupReport()
        let episodes = (try? modelContext.fetch(FetchDescriptor<Episode>())) ?? []
        for episode in episodes {
            report.transcriptSegmentsRemoved += removeDuplicateTranscriptSegments(for: episode)
            report.adMarkersRemoved += removeDuplicateAdMarkers(for: episode)
            episode.activeAdMarkerCount = episode.adMarkers.filter { !$0.isDeleted }.count
        }

        let podcasts = (try? modelContext.fetch(FetchDescriptor<Podcast>())) ?? []
        for podcast in podcasts {
            refreshPodcastDerivedState(podcast)
        }
        return report
    }

    private func merge(duplicatePodcast: Podcast, into canonical: Podcast) {
        if canonical.title.isEmpty { canonical.title = duplicatePodcast.title }
        canonical.author = canonical.author ?? duplicatePodcast.author
        canonical.summary = canonical.summary ?? duplicatePodcast.summary
        canonical.artworkURL = canonical.artworkURL ?? duplicatePodcast.artworkURL
        canonical.lastFetched = [canonical.lastFetched, duplicatePodcast.lastFetched].compactMap { $0 }.max()
        canonical.cachedArtworkFilename = canonical.cachedArtworkFilename ?? duplicatePodcast.cachedArtworkFilename
        canonical.cachedArtworkSourceURL = canonical.cachedArtworkSourceURL ?? duplicatePodcast.cachedArtworkSourceURL
        canonical.customPlaybackSpeed = canonical.customPlaybackSpeed ?? duplicatePodcast.customPlaybackSpeed
        canonical.autoDownloadEnabled = canonical.autoDownloadEnabled || duplicatePodcast.autoDownloadEnabled
        canonical.aiProcessingEnabled = canonical.aiProcessingEnabled || duplicatePodcast.aiProcessingEnabled

        for episode in duplicatePodcast.episodes {
            episode.podcast = canonical
            episode.syncPodcastSnapshot(from: canonical)
        }
    }

    private func merge(duplicateEpisode: Episode, into canonical: Episode) {
        if canonical.title.isEmpty { canonical.title = duplicateEpisode.title }
        canonical.episodeDescription = canonical.episodeDescription ?? duplicateEpisode.episodeDescription
        canonical.publishedAt = canonical.publishedAt ?? duplicateEpisode.publishedAt
        canonical.duration = canonical.duration ?? duplicateEpisode.duration
        canonical.audioMimeType = canonical.audioMimeType ?? duplicateEpisode.audioMimeType
        canonical.fileSizeBytes = canonical.fileSizeBytes ?? duplicateEpisode.fileSizeBytes
        canonical.processingError = canonical.processingError ?? duplicateEpisode.processingError
        canonical.processingProgress = max(canonical.processingProgress, duplicateEpisode.processingProgress)
        canonical.processingCurrent = canonical.processingCurrent ?? duplicateEpisode.processingCurrent
        canonical.processingTotal = canonical.processingTotal ?? duplicateEpisode.processingTotal
        canonical.playbackPosition = max(canonical.playbackPosition, duplicateEpisode.playbackPosition)
        canonical.isPlayed = canonical.isPlayed || duplicateEpisode.isPlayed
        canonical.datePlayed = [canonical.datePlayed, duplicateEpisode.datePlayed].compactMap { $0 }.max()

        if canonical.localFilename == nil {
            canonical.localFilename = duplicateEpisode.localFilename
            canonical.fileSizeBytes = duplicateEpisode.fileSizeBytes
            duplicateEpisode.localFilename = nil
            duplicateEpisode.fileSizeBytes = nil
        } else if duplicateEpisode.localFilename != nil,
                  duplicateEpisode.localFilename != canonical.localFilename,
                  let duplicateURL = duplicateEpisode.localFileURL {
            try? FileManager.default.removeItem(at: duplicateURL)
        }

        if processingStateRank(duplicateEpisode.processingState) > processingStateRank(canonical.processingState) {
            canonical.processingState = duplicateEpisode.processingState
        }

        for segment in duplicateEpisode.transcript {
            segment.episode = canonical
        }
        for marker in duplicateEpisode.adMarkers {
            marker.episode = canonical
        }
        moveQueueItems(from: duplicateEpisode, to: canonical)

        if let podcast = canonical.podcast {
            canonical.syncPodcastSnapshot(from: podcast)
        }
    }

    private func moveQueueItems(from duplicate: Episode, to canonical: Episode) {
        let queueItems = (try? modelContext.fetch(FetchDescriptor<QueueItem>())) ?? []
        for item in queueItems where item.episode?.persistentModelID == duplicate.persistentModelID {
            item.episode = canonical
        }
    }

    private func removeDuplicateTranscriptSegments(for episode: Episode) -> Int {
        var seen = Set<String>()
        var removed = 0
        for segment in episode.transcript.sorted(by: sortTranscriptSegments) {
            let key = "\(roundedMillis(segment.startSeconds))|\(roundedMillis(segment.endSeconds))|\(segment.text)"
            guard seen.insert(key).inserted else {
                modelContext.delete(segment)
                removed += 1
                continue
            }
        }
        return removed
    }

    private func removeDuplicateAdMarkers(for episode: Episode) -> Int {
        var seen = Set<String>()
        var removed = 0
        for marker in episode.adMarkers.sorted(by: sortAdMarkers) {
            let key = [
                "\(roundedMillis(marker.startSeconds))",
                "\(roundedMillis(marker.endSeconds))",
                marker.kindRaw,
                marker.summary,
                marker.manuallyEdited ? "manual" : "auto",
                marker.isDeleted ? "deleted" : "active"
            ].joined(separator: "|")
            guard seen.insert(key).inserted else {
                modelContext.delete(marker)
                removed += 1
                continue
            }
        }
        return removed
    }

    private func refreshPodcastDerivedState(_ podcast: Podcast) {
        podcast.latestEpisodeAt = podcast.episodes.compactMap(\.publishedAt).max()
        podcast.syncEpisodeSnapshots()
    }

    private func podcastScore(_ podcast: Podcast) -> Int {
        var score = podcast.episodes.count * 100
        if podcast.lastFetched != nil { score += 20 }
        if podcast.cachedArtworkFilename != nil { score += 10 }
        if !podcast.title.isEmpty { score += 1 }
        return score
    }

    private func episodeScore(_ episode: Episode) -> Int {
        var score = processingStateRank(episode.processingState) * 1_000
        if episode.hasLocalFile { score += 500 }
        if episode.localFilename != nil { score += 250 }
        score += min(episode.adMarkers.count, 100) * 5
        score += min(episode.transcript.count, 100)
        if episode.fileSizeBytes != nil { score += 10 }
        if episode.publishedAt != nil { score += 1 }
        return score
    }

    private func processingStateRank(_ state: EpisodeProcessingState) -> Int {
        switch state {
        case .ready: 6
        case .detectingAds: 5
        case .uploading, .transcribing: 4
        case .downloaded: 3
        case .downloading: 2
        case .failed: 1
        case .new: 0
        }
    }

    private func episodeDedupeKey(_ episode: Episode) -> String {
        let podcastKey = episode.podcast?.feedURL.absoluteString ?? "orphan"
        return "\(podcastKey)|\(episode.guid)"
    }

    private func roundedMillis(_ value: Double) -> Int {
        Int((value * 1_000).rounded())
    }

    private func sortTranscriptSegments(_ lhs: TranscriptSegment, _ rhs: TranscriptSegment) -> Bool {
        if lhs.startSeconds != rhs.startSeconds { return lhs.startSeconds < rhs.startSeconds }
        if lhs.endSeconds != rhs.endSeconds { return lhs.endSeconds < rhs.endSeconds }
        return lhs.text < rhs.text
    }

    private func sortAdMarkers(_ lhs: AdMarker, _ rhs: AdMarker) -> Bool {
        if lhs.startSeconds != rhs.startSeconds { return lhs.startSeconds < rhs.startSeconds }
        if lhs.endSeconds != rhs.endSeconds { return lhs.endSeconds < rhs.endSeconds }
        if lhs.manuallyEdited != rhs.manuallyEdited { return lhs.manuallyEdited && !rhs.manuallyEdited }
        return lhs.summary < rhs.summary
    }
}
