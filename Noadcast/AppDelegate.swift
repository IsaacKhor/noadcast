import UIKit

/// Minimal `UIApplicationDelegate` whose only job is to receive the system's
/// background-URLSession completion handler. iOS hands this to us when it
/// relaunches the app to deliver download events; `DownloadService` invokes it
/// inside `urlSessionDidFinishEvents(forBackgroundURLSession:)` so the system
/// knows we're done processing.
final class AppDelegate: NSObject, UIApplicationDelegate {
    func application(
        _ application: UIApplication,
        handleEventsForBackgroundURLSession identifier: String,
        completionHandler: @escaping () -> Void
    ) {
        guard identifier == DownloadService.backgroundSessionIdentifier else {
            completionHandler()
            return
        }
        DownloadService.shared.storePendingBackgroundCompletion(completionHandler)
    }
}
