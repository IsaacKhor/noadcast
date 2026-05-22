import UIKit

/// Minimal `UIApplicationDelegate` whose only job is to receive the system's
/// background-URLSession completion handler. iOS hands this to us when it
/// relaunches the app to deliver download or cloud-upload events; the
/// matching service invokes it inside
/// `urlSessionDidFinishEvents(forBackgroundURLSession:)` so the system
/// knows we're done processing.
final class AppDelegate: NSObject, UIApplicationDelegate {
    func application(
        _ application: UIApplication,
        handleEventsForBackgroundURLSession identifier: String,
        completionHandler: @escaping () -> Void
    ) {
        switch identifier {
        case DownloadService.backgroundSessionIdentifier:
            DownloadService.shared.storePendingBackgroundCompletion(completionHandler)
        case CloudTranscriptionService.backgroundSessionIdentifier:
            CloudTranscriptionService.shared.storePendingBackgroundCompletion(completionHandler)
        default:
            completionHandler()
        }
    }
}
