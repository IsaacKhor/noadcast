import Foundation
import os

enum DownloadError: LocalizedError {
    case badResponse(Int)
    case noLocalURL
    case moveFailed(Error)
    var errorDescription: String? {
        switch self {
        case .badResponse(let code): "Server returned HTTP \(code)."
        case .noLocalURL: "Download finished but produced no file."
        case .moveFailed(let err): "Couldn't move the downloaded file: \(err.localizedDescription)"
        }
    }
}

struct DownloadProgress: Sendable {
    let bytesWritten: Int64
    let totalBytes: Int64?
    var fraction: Double {
        guard let total = totalBytes, total > 0 else { return 0 }
        return Double(bytesWritten) / Double(total)
    }
}

enum DownloadEvent: Sendable {
    case progress(DownloadProgress)
    case completed(filename: String, sizeBytes: Int64)
}

/// Downloads remote audio files via a **background** `URLSession` so transfers
/// continue while the app is suspended.
///
/// The async `URLSession` APIs (`bytes(from:)`, `download(from:)`) aren't
/// supported on background configurations, so the delegate is driven manually
/// and its callbacks are bridged into an `AsyncThrowingStream`.
///
/// All public methods and the `URLSessionDownloadDelegate` conformance are
/// `nonisolated`. Mutable state (`continuations`, `pendingBackgroundCompletion`)
/// is `nonisolated(unsafe)` and guarded by `lock`.
nonisolated final class DownloadService: NSObject, @unchecked Sendable {
    static let shared = DownloadService()

    static let backgroundSessionIdentifier = "com.isaackhor.Noadcast.background-downloads"

    private var continuations: [Int: AsyncThrowingStream<DownloadEvent, Error>.Continuation] = [:]
    private var progressSnapshots: [Int: ProgressSnapshot] = [:]
    private var pendingBackgroundCompletion: BackgroundCompletion?
    private let lock = NSLock()

    private var sessionStorage: URLSession!
    var session: URLSession { sessionStorage }

    private struct ProgressSnapshot {
        var lastYieldUptime: TimeInterval
        var lastBytesWritten: Int64
        var lastTotalBytes: Int64?
    }

    private static let progressThrottleInterval: TimeInterval = 0.5
    private static let progressThrottleBytes: Int64 = 512 * 1024
    private static let progressThrottleFraction: Double = 0.01

    override private init() {
        super.init()
        let config = URLSessionConfiguration.background(withIdentifier: Self.backgroundSessionIdentifier)
        config.sessionSendsLaunchEvents = true
        config.isDiscretionary = false
        config.allowsCellularAccess = true  // gated upstream by AutoDownloadPolicy.
        sessionStorage = URLSession(configuration: config, delegate: self, delegateQueue: nil)
    }

    /// Stores the completion handler iOS hands us when relaunching for
    /// background events. Called from `AppDelegate`.
    func storePendingBackgroundCompletion(_ handler: @escaping () -> Void) {
        lock.lock()
        pendingBackgroundCompletion = BackgroundCompletion(handler)
        lock.unlock()
    }

    func download(
        from remoteURL: URL,
        suggestedFilename: String
    ) -> AsyncThrowingStream<DownloadEvent, Error> {
        AsyncThrowingStream { continuation in
            let task = session.downloadTask(with: remoteURL)
            task.taskDescription = suggestedFilename

            lock.lock()
            continuations[task.taskIdentifier] = continuation
            lock.unlock()

            let taskID = task.taskIdentifier
            continuation.onTermination = { [weak self] termination in
                guard let self else { return }
                if case .cancelled = termination {
                    task.cancel()
                }
                self.lock.lock()
                self.continuations.removeValue(forKey: taskID)
                self.progressSnapshots.removeValue(forKey: taskID)
                self.lock.unlock()
            }

            task.resume()
        }
    }

    static func suggestedFilename(for guid: String, mimeType: String?) -> String {
        let ext: String
        switch mimeType?.lowercased() {
        case let m? where m.contains("mpeg"): ext = "mp3"
        case let m? where m.contains("mp4"), let m? where m.contains("m4a"): ext = "m4a"
        case let m? where m.contains("aac"): ext = "aac"
        case let m? where m.contains("ogg"): ext = "ogg"
        case let m? where m.contains("wav"): ext = "wav"
        default: ext = "mp3"
        }
        let slug = guid.compactMap { ch -> Character? in
            (ch.isLetter || ch.isNumber) ? ch : "_"
        }
        return "\(String(slug.prefix(80))).\(ext)"
    }

    // MARK: - Internal helpers

    private func takeContinuation(
        for taskID: Int
    ) -> AsyncThrowingStream<DownloadEvent, Error>.Continuation? {
        lock.lock()
        defer { lock.unlock() }
        progressSnapshots.removeValue(forKey: taskID)
        return continuations.removeValue(forKey: taskID)
    }

    private func peekContinuation(
        for taskID: Int
    ) -> AsyncThrowingStream<DownloadEvent, Error>.Continuation? {
        lock.lock()
        defer { lock.unlock() }
        return continuations[taskID]
    }

    private func shouldYieldProgress(taskID: Int, progress: DownloadProgress) -> Bool {
        let now = ProcessInfo.processInfo.systemUptime
        lock.lock()
        defer { lock.unlock() }

        guard let previous = progressSnapshots[taskID] else {
            progressSnapshots[taskID] = ProgressSnapshot(
                lastYieldUptime: now,
                lastBytesWritten: progress.bytesWritten,
                lastTotalBytes: progress.totalBytes
            )
            return true
        }

        let isComplete = progress.totalBytes.map { progress.bytesWritten >= $0 } ?? false
        let totalBecameKnown = previous.lastTotalBytes == nil && progress.totalBytes != nil
        let elapsed = now - previous.lastYieldUptime
        let byteDelta = progress.bytesWritten - previous.lastBytesWritten
        let fractionDelta: Double = {
            guard let total = progress.totalBytes, total > 0 else { return 0 }
            return Double(byteDelta) / Double(total)
        }()

        let shouldYield = isComplete
            || totalBecameKnown
            || (elapsed >= Self.progressThrottleInterval
                && (byteDelta >= Self.progressThrottleBytes
                    || fractionDelta >= Self.progressThrottleFraction))

        if shouldYield {
            progressSnapshots[taskID] = ProgressSnapshot(
                lastYieldUptime: now,
                lastBytesWritten: progress.bytesWritten,
                lastTotalBytes: progress.totalBytes
            )
        }
        return shouldYield
    }
}

/// Holds a non-`Sendable` UIKit completion handler so it can be stored in
/// `DownloadService` (which is `@unchecked Sendable`) and invoked later on
/// the main queue. The handler is set once at construction (`let`), so
/// reading it from any thread is safe.
nonisolated private final class BackgroundCompletion: @unchecked Sendable {
    let handler: () -> Void
    init(_ handler: @escaping () -> Void) { self.handler = handler }
}

// MARK: - URLSession delegate

extension DownloadService: @preconcurrency URLSessionDownloadDelegate {
    func urlSession(
        _ session: URLSession,
        downloadTask: URLSessionDownloadTask,
        didWriteData bytesWritten: Int64,
        totalBytesWritten: Int64,
        totalBytesExpectedToWrite: Int64
    ) {
        let total: Int64? = totalBytesExpectedToWrite > 0 ? totalBytesExpectedToWrite : nil
        let progress = DownloadProgress(bytesWritten: totalBytesWritten, totalBytes: total)
        guard shouldYieldProgress(taskID: downloadTask.taskIdentifier, progress: progress) else {
            return
        }
        peekContinuation(for: downloadTask.taskIdentifier)?.yield(.progress(
            progress
        ))
    }

    func urlSession(
        _ session: URLSession,
        downloadTask: URLSessionDownloadTask,
        didFinishDownloadingTo location: URL
    ) {
        // This callback runs synchronously on the delegate queue; the temp file
        // at `location` is deleted as soon as we return, so the move must
        // happen here, not in a later Task.
        let filename = downloadTask.taskDescription
            ?? "episode_\(downloadTask.taskIdentifier).mp3"
        let destination = Episode.episodesDirectory.appendingPathComponent(filename)

        var moveError: Error?
        var movedSize: Int64 = 0
        do {
            if FileManager.default.fileExists(atPath: destination.path) {
                try FileManager.default.removeItem(at: destination)
            }
            try FileManager.default.moveItem(at: location, to: destination)
            let attrs = try FileManager.default.attributesOfItem(atPath: destination.path)
            movedSize = (attrs[.size] as? NSNumber)?.int64Value ?? 0
        } catch {
            moveError = error
        }

        let taskID = downloadTask.taskIdentifier
        let response = downloadTask.response as? HTTPURLResponse
        if let status = response?.statusCode, !(200..<300).contains(status) {
            try? FileManager.default.removeItem(at: destination)
            takeContinuation(for: taskID)?.finish(throwing: DownloadError.badResponse(status))
            return
        }
        if let moveError {
            takeContinuation(for: taskID)?.finish(throwing: DownloadError.moveFailed(moveError))
            return
        }

        // `.completed` event here; the matching `finish()` happens in
        // `didCompleteWithError` so completion always follows the final event.
        peekContinuation(for: taskID)?.yield(.completed(filename: filename, sizeBytes: movedSize))
    }

    func urlSession(
        _ session: URLSession,
        task: URLSessionTask,
        didCompleteWithError error: Error?
    ) {
        let cont = takeContinuation(for: task.taskIdentifier)
        if let error {
            let urlStr = task.originalRequest?.url?.absoluteString ?? "?"
            Log.download.error("Task failed url=\(urlStr, privacy: .public) \(Log.describe(error), privacy: .public)")
            cont?.finish(throwing: error)
        } else {
            cont?.finish()
        }
    }

    func urlSessionDidFinishEvents(forBackgroundURLSession session: URLSession) {
        lock.lock()
        let pending = pendingBackgroundCompletion
        pendingBackgroundCompletion = nil
        lock.unlock()
        DispatchQueue.main.async { pending?.handler() }
    }
}
