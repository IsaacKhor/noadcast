import Foundation
import SwiftData
import os

/// Off-main-thread backfill for cached `Podcast.latestEpisodeAt` values.
///
/// New podcasts get the cache populated synchronously inside
/// `SubscriptionService.importEpisodes`. This actor handles the legacy case:
/// rows that were inserted before the cache field existed (i.e. on first
/// launch after the schema migration). Walking the episode relationship to
/// find the max `publishedAt` faults every `Episode` row, which is exactly
/// the work that used to hang the main thread for ~800 ms — `@ModelActor`
/// lets us do it on a dedicated context off main.
@ModelActor
actor MetadataBackfillActor {
    func backfillLatestEpisodeDates() {
        let state = Log.signposter.beginInterval("MetadataBackfill.latestEpisodeAt")
        defer { Log.signposter.endInterval("MetadataBackfill.latestEpisodeAt", state) }

        let descriptor = FetchDescriptor<Podcast>(
            predicate: #Predicate { $0.latestEpisodeAt == nil }
        )
        guard let candidates = try? modelContext.fetch(descriptor), !candidates.isEmpty else {
            return
        }
        Log.startup.info("Backfilling latestEpisodeAt for \(candidates.count) podcast(s)")
        for podcast in candidates {
            let max = podcast.episodes.compactMap(\.publishedAt).max()
            podcast.latestEpisodeAt = max
        }
        try? modelContext.save()
    }
}
