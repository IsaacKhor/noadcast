import Foundation
import CryptoKit
import SwiftData
import os

/// Caches each podcast's artwork image on disk so it doesn't have to be
/// re-downloaded on every cold launch / list scroll / Now Playing load.
/// The cache is invalidated and refreshed lazily by `cache(for:)`, which
/// `SubscriptionService` calls during subscribe + refresh.
@MainActor
final class ArtworkService {
    static let shared = ArtworkService()

    /// `Application Support/artwork/` — created on first access.
    nonisolated static let artworkDirectory: URL = {
        let base = try! FileManager.default.url(
            for: .applicationSupportDirectory,
            in: .userDomainMask,
            appropriateFor: nil,
            create: true
        )
        let dir = base.appendingPathComponent("artwork", isDirectory: true)
        if !FileManager.default.fileExists(atPath: dir.path) {
            try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        }
        return dir
    }()

    nonisolated static func localURL(filename: String) -> URL {
        artworkDirectory.appendingPathComponent(filename)
    }

    /// Downloads `podcast.artworkURL` (if any) and writes it to a stable
    /// location under `artworkDirectory`. No-ops when the cache is already
    /// current (same source URL, file still on disk).
    func cache(for podcast: Podcast) async {
        guard let url = podcast.artworkURL else {
            // Podcast had artwork before but doesn't anymore — drop the cached file.
            if let filename = podcast.cachedArtworkFilename {
                try? FileManager.default.removeItem(at: Self.localURL(filename: filename))
            }
            podcast.cachedArtworkFilename = nil
            podcast.cachedArtworkSourceURL = nil
            return
        }
        // Skip when the cache is up to date and the file still exists.
        if podcast.cachedArtworkSourceURL == url,
           let filename = podcast.cachedArtworkFilename,
           FileManager.default.fileExists(atPath: Self.localURL(filename: filename).path) {
            return
        }
        do {
            let (data, _) = try await URLSession.shared.data(from: url)
            let filename = Self.filename(for: podcast, sourceURL: url)
            let destination = Self.localURL(filename: filename)
            try data.write(to: destination, options: .atomic)
            podcast.cachedArtworkFilename = filename
            podcast.cachedArtworkSourceURL = url
        } catch {
            Log.feed.notice("Artwork cache failed for \"\(podcast.title, privacy: .public)\": \(error.localizedDescription, privacy: .public)")
        }
    }

    /// Caches artwork for every podcast that doesn't already have a fresh
    /// local copy. Called once at launch so podcasts subscribed before
    /// `cachedArtworkFilename` existed don't keep going back to the
    /// network. `cache(for:)` is idempotent and no-ops when the cache is
    /// already current, so it's safe to call against the entire library.
    func backfillAllPodcasts(context: ModelContext) async {
        let podcasts = (try? context.fetch(FetchDescriptor<Podcast>())) ?? []
        // Fire off all downloads concurrently — `URLSession` pools the
        // connections itself, and the per-podcast `cache(for:)` short-
        // circuits on an up-to-date cache so the cost is bounded.
        await withTaskGroup(of: Void.self) { group in
            for podcast in podcasts {
                group.addTask { @MainActor in
                    await self.cache(for: podcast)
                }
            }
        }
        try? context.save()
    }

    /// Removes the cached artwork file for a podcast (called from
    /// `SubscriptionService.unsubscribe`).
    func deleteCache(for podcast: Podcast) {
        if let filename = podcast.cachedArtworkFilename {
            try? FileManager.default.removeItem(at: Self.localURL(filename: filename))
        }
        podcast.cachedArtworkFilename = nil
        podcast.cachedArtworkSourceURL = nil
    }

    /// Stable filename keyed on the podcast's `feedURL` so a re-cache simply
    /// overwrites the previous file. Extension copied from the artwork URL
    /// (defaulting to `jpg`).
    private static func filename(for podcast: Podcast, sourceURL: URL) -> String {
        let hash = SHA256.hash(data: Data(podcast.feedURL.absoluteString.utf8))
        let hex = hash.prefix(8).map { String(format: "%02x", $0) }.joined()
        let ext = sourceURL.pathExtension.isEmpty ? "jpg" : sourceURL.pathExtension
        return "\(hex).\(ext)"
    }
}
