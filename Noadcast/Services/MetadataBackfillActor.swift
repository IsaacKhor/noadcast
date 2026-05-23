import Foundation
import SwiftData
import os

/// Off-main-thread backfill for cached podcast/episode metadata.
///
/// New podcasts get the caches populated synchronously inside services. This
/// actor handles legacy rows after schema migrations. Walking relationships
/// faults rows, which is exactly the work we do not want on the scrolling
/// path, so `@ModelActor` does it on a dedicated context off main.
@ModelActor
actor MetadataBackfillActor {
    func backfillLatestEpisodeDates() {
        let state = Log.signposter.beginInterval("MetadataBackfill.derivedMetadata")
        defer { Log.signposter.endInterval("MetadataBackfill.derivedMetadata", state) }

        let descriptor = FetchDescriptor<Podcast>()
        guard let candidates = try? modelContext.fetch(descriptor), !candidates.isEmpty else {
            return
        }
        Log.startup.info("Backfilling derived metadata for \(candidates.count) podcast(s)")
        for podcast in candidates {
            let episodes = podcast.episodes
            if podcast.latestEpisodeAt == nil {
                podcast.latestEpisodeAt = episodes.compactMap(\.publishedAt).max()
            }
            podcast.episodeCount = episodes.count
            for episode in episodes {
                episode.syncPodcastSnapshot(from: podcast)
                episode.activeAdMarkerCount = episode.adMarkers.filter { !$0.isDeleted }.count
            }
        }
        try? modelContext.save()
    }
}
