import SwiftUI
import UIKit
import ImageIO

/// Tiny in-memory image cache backing `CachedArtworkImage`.
///
/// `AsyncImage` re-decodes each time a row leaves and re-enters the visible
/// region, even when the source URL points to a local file. That decode is
/// the dominant cost while scrolling a long episode list, so we cache the
/// already-decoded `UIImage` keyed on `URL + target pixel size` (so a
/// 56pt-row hit doesn't clobber the 80pt detail header). `NSCache` evicts
/// entries automatically under memory pressure.
@MainActor
final class ArtworkMemoryCache {
    static let shared = ArtworkMemoryCache()
    private let cache: NSCache<NSString, UIImage> = {
        let c = NSCache<NSString, UIImage>()
        c.countLimit = 200
        return c
    }()

    func image(forKey key: NSString) -> UIImage? { cache.object(forKey: key) }
    func set(_ image: UIImage, forKey key: NSString) { cache.setObject(image, forKey: key) }
}

/// Drop-in replacement for `AsyncImage` that hits a shared in-memory cache
/// first and, on miss, **downsamples** the source via ImageIO so the
/// cached `UIImage` already matches the on-screen pixel size. That means
/// the render-time `.resizable()` + `.frame` is effectively a no-op
/// instead of a Core Animation downscale every frame — the big win while
/// scrolling lists of podcast art.
///
/// `size` is the point side length of the square frame the artwork will
/// be drawn into; the loader multiplies by `displayScale` to get the
/// target pixel size and asks `CGImageSource` for a thumbnail at most
/// that big.
struct CachedArtworkImage: View {
    let url: URL?
    /// Point side length of the destination frame. Callers still apply
    /// `.frame(width: size, height: size)` themselves; we only need the
    /// value here so we know how much to downsample.
    let size: CGFloat

    @Environment(\.displayScale) private var displayScale
    @State private var image: UIImage?
    @State private var loadedKey: NSString?
    @State private var loadTask: Task<Void, Never>?

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
        .onDisappear {
            loadTask?.cancel()
            loadTask = nil
        }
    }

    private var pixelSize: CGFloat {
        // `displayScale` is 0 in some preview / non-onscreen contexts;
        // fall back to 3x so we never ask for a 0-pixel thumbnail.
        let scale = displayScale > 0 ? displayScale : 3
        return (size * scale).rounded()
    }

    private func cacheKey(for url: URL) -> NSString {
        "\(url.absoluteString)#\(Int(pixelSize))" as NSString
    }

    private func load() {
        guard let url else {
            loadTask?.cancel()
            loadTask = nil
            image = nil
            loadedKey = nil
            return
        }
        let key = cacheKey(for: url)
        if loadedKey == key, image != nil { return }
        loadTask?.cancel()
        loadedKey = key
        if let cached = ArtworkMemoryCache.shared.image(forKey: key) {
            image = cached
            return
        }
        let targetPixelSize = pixelSize
        image = nil
        loadTask = Task { @MainActor in
            do {
                let loaded: UIImage?
                if url.isFileURL {
                    loaded = await Self.downsampledLocalImage(fileURL: url, maxPixelSize: targetPixelSize)
                } else {
                    let (data, _) = try await URLSession.shared.data(from: url)
                    if Task.isCancelled { return }
                    loaded = await Self.downsampledRemoteImage(data: data, maxPixelSize: targetPixelSize)
                }
                if Task.isCancelled || self.loadedKey != key { return }
                guard let loaded else { return }
                ArtworkMemoryCache.shared.set(loaded, forKey: key)
                self.image = loaded
            } catch {
                // Best-effort — leave the placeholder visible.
            }
        }
    }

    nonisolated private static func downsampledLocalImage(fileURL: URL, maxPixelSize: CGFloat) async -> UIImage? {
        await Task.detached(priority: .utility) {
            downsampledImage(fileURL: fileURL, maxPixelSize: maxPixelSize)
        }.value
    }

    nonisolated private static func downsampledRemoteImage(data: Data, maxPixelSize: CGFloat) async -> UIImage? {
        await Task.detached(priority: .utility) {
            downsampledImage(data: data, maxPixelSize: maxPixelSize)
        }.value
    }

    nonisolated private static func downsampledImage(fileURL: URL, maxPixelSize: CGFloat) -> UIImage? {
        guard let source = CGImageSourceCreateWithURL(fileURL as CFURL, nil) else { return nil }
        return downsample(source: source, maxPixelSize: maxPixelSize)
    }

    nonisolated private static func downsampledImage(data: Data, maxPixelSize: CGFloat) -> UIImage? {
        guard let source = CGImageSourceCreateWithData(data as CFData, nil) else { return nil }
        return downsample(source: source, maxPixelSize: maxPixelSize)
    }

    nonisolated private static func downsample(source: CGImageSource, maxPixelSize: CGFloat) -> UIImage? {
        let options: [CFString: Any] = [
            kCGImageSourceCreateThumbnailFromImageAlways: true,
            kCGImageSourceCreateThumbnailWithTransform: true,
            kCGImageSourceShouldCacheImmediately: true,
            kCGImageSourceThumbnailMaxPixelSize: maxPixelSize,
        ]
        guard let cgImage = CGImageSourceCreateThumbnailAtIndex(source, 0, options as CFDictionary) else {
            return nil
        }
        return UIImage(cgImage: cgImage)
    }
}
