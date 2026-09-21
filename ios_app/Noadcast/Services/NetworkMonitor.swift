import Foundation
import Network
import os

/// Observes reachability + Wi-Fi vs cellular. Used to gate auto-download.
@MainActor
@Observable
final class NetworkMonitor {
    static let shared = NetworkMonitor()

    private(set) var isOnline: Bool = false
    private(set) var isWiFi: Bool = false

    private let monitor: NWPathMonitor
    private let queue = DispatchQueue(label: "NetworkMonitor")

    init() {
        let initState = Log.signposter.beginInterval("NetworkMonitor.init")
        monitor = NWPathMonitor()
        monitor.pathUpdateHandler = { [weak self] path in
            let online = path.status == .satisfied
            let wifi = path.usesInterfaceType(.wifi)
            Task { @MainActor [weak self] in
                self?.isOnline = online
                self?.isWiFi = wifi
            }
        }
        Log.signposter.withIntervalSignpost("NWPathMonitor.start") {
            monitor.start(queue: queue)
        }
        Log.signposter.endInterval("NetworkMonitor.init", initState)
    }

    func canAutoDownload(under policy: AutoDownloadPolicy) -> Bool {
        guard isOnline else { return false }
        switch policy {
        case .manualOnly: return false
        case .wifiOnly: return isWiFi
        case .anyNetwork: return true
        }
    }
}
