import SwiftUI
import UIKit

/// Tiny in-memory image cache backing `CachedArtworkImage`.
///
/// `AsyncImage` re-decodes each time a row leaves and re-enters the visible
/// region, even when the source URL points to a local file. That decode is
/// the dominant cost while scrolling a long episode list, so we cache the
/// already-decoded `UIImage` keyed on the source URL. `NSCache` evicts
/// entries automatically under memory pressure.
@MainActor
final class ArtworkMemoryCache {
    static let shared = ArtworkMemoryCache()
    private let cache: NSCache<NSURL, UIImage> = {
        let c = NSCache<NSURL, UIImage>()
        c.countLimit = 200
        return c
    }()

    func image(for url: URL) -> UIImage? { cache.object(forKey: url as NSURL) }
    func set(_ image: UIImage, for url: URL) { cache.setObject(image, forKey: url as NSURL) }
}

/// Drop-in replacement for `AsyncImage` that hits a shared in-memory cache
/// first. For `file://` URLs (the path `Podcast.artworkDisplayURL` returns
/// once `ArtworkService` has cached the image to disk) it loads
/// synchronously so the row's first frame already has the artwork. Remote
/// URLs are fetched async; once decoded they go into the cache so the next
/// scroll-onto-screen is instant.
struct CachedArtworkImage: View {
    let url: URL?

    @State private var image: UIImage?
    @State private var loadedURL: URL?

    var body: some View {
        Group {
            if let image {
                Image(uiImage: image).resizable()
            } else {
                Color.gray.opacity(0.2)
            }
        }
        .onAppear(perform: load)
        .onChange(of: url) { _, _ in load() }
    }

    private func load() {
        guard let url else {
            image = nil
            loadedURL = nil
            return
        }
        if loadedURL == url, image != nil { return }
        loadedURL = url
        if let cached = ArtworkMemoryCache.shared.image(for: url) {
            image = cached
            return
        }
        if url.isFileURL {
            if let loaded = UIImage(contentsOfFile: url.path) {
                ArtworkMemoryCache.shared.set(loaded, for: url)
                image = loaded
            } else {
                image = nil
            }
            return
        }
        // Remote URL — fetch off the main thread, then publish back.
        Task { @MainActor in
            do {
                let (data, _) = try await URLSession.shared.data(from: url)
                if self.loadedURL != url { return }  // a newer URL has been requested
                guard let loaded = UIImage(data: data) else { return }
                ArtworkMemoryCache.shared.set(loaded, for: url)
                self.image = loaded
            } catch {
                // Best-effort — leave the placeholder visible.
            }
        }
    }
}
