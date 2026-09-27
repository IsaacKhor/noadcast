import SwiftUI
import SwiftData
import UniformTypeIdentifiers

struct SettingsView: View {
    @Environment(\.modelContext) private var context
    @Query private var settingsList: [AppSettings]

    /// Device-local preference in `UserDefaults` (not `AppSettings`, whose
    /// writes invalidate every settings query).
    @AppStorage(StreamingPolicy.storageKey) private var streamingPolicy: StreamingPolicy = StreamingPolicy.defaultValue
    /// The raw `APIConfiguration.baseURLString`. Read through `@AppStorage`
    /// so the Server row updates as soon as the setup screen saves a new
    /// address; `APIConfiguration` itself is not observable.
    @AppStorage(APIConfiguration.baseURLDefaultsKey) private var storedServerAddress: String?

    @State private var showOPMLPicker = false
    @State private var isImportingOPML = false
    @State private var alertTitle = ""
    @State private var alertMessage: String?
    /// The ad-detection value just picked, shown until the server call
    /// settles. `setGlobalAdAnalysis` writes its optimistic value on a later
    /// main-actor turn, so without this the switch would bounce for a frame.
    @State private var pendingAdAnalysis: Bool?
    /// Only the latest toggle request clears `pendingAdAnalysis`.
    @State private var adAnalysisRequestCount = 0

    private var settings: AppSettings? { settingsList.first }

    /// Same normalisation as `APIConfiguration.baseURL`.
    private var serverURL: URL? {
        storedServerAddress.flatMap { APIConfiguration.normalizedBaseURL(from: $0) }
    }

    private var isConfigured: Bool { serverURL != nil }

    /// `host[:port][/prefix]` of the configured server.
    private var serverDisplayAddress: String? {
        guard let url = serverURL else { return nil }
        guard let components = URLComponents(url: url, resolvingAgainstBaseURL: false),
              let host = components.host, !host.isEmpty
        else { return url.absoluteString }
        var display = host
        if let port = components.port {
            display += ":\(port)"
        }
        display += components.path
        return display
    }

    var body: some View {
        NavigationStack {
            Form {
                serverSection
                if let s = settings {
                    timeSavedSection(settings: s)
                    usageHistorySection
                    playbackSection(settings: s)
                    skippingSection(settings: s)
                }
                streamingSection
                if let s = settings {
                    downloadsSection(settings: s)
                    adAnalysisSection(settings: s)
                }
                importSection
            }
            .navigationTitle("Settings")
            .navigationBarTitleDisplayMode(.inline)
            .fileImporter(
                isPresented: $showOPMLPicker,
                allowedContentTypes: [.xml, UTType(filenameExtension: "opml") ?? .xml]
            ) { result in
                Task { await handleOPMLImport(result: result) }
            }
            .alert(alertTitle, isPresented: .constant(alertMessage != nil), actions: {
                Button("OK") { alertMessage = nil }
            }, message: {
                Text(alertMessage ?? "")
            })
        }
    }

    // MARK: - Server

    @ViewBuilder
    private var serverSection: some View {
        Section {
            NavigationLink {
                ServerSetupView()
            } label: {
                Label(serverDisplayAddress ?? "Not connected", systemImage: "server.rack")
                    .lineLimit(1)
                    .truncationMode(.middle)
            }
            ServerStatusRow(isConfigured: isConfigured)
            if let migrationStatus = DeviceStateRestoreService.shared.statusLine {
                Text(migrationStatus)
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        } header: {
            Text("Server")
        }
    }

    /// One-line sync status under the Server row. A separate view so sync
    /// state changes (`isSyncing` flips on every sync) re-render only this row.
    private struct ServerStatusRow: View {
        let isConfigured: Bool

        var body: some View {
            let sync = SyncService.shared
            if !isConfigured {
                Text("Add your server's address to sync your library.")
                    .foregroundStyle(.secondary)
            } else if sync.authFailed {
                Label("Token rejected — update it", systemImage: "exclamationmark.triangle.fill")
                    .foregroundStyle(.red)
            } else if sync.isSyncing {
                HStack(spacing: 8) {
                    ProgressView()
                    Text("Syncing…")
                        .foregroundStyle(.secondary)
                }
            } else if let lastError = sync.lastError {
                Label {
                    Text(verbatim: "Sync failed: \(lastError)")
                } icon: {
                    Image(systemName: "exclamationmark.circle")
                }
                .foregroundStyle(.red)
            } else if let lastSyncAt = sync.lastSyncAt {
                // Re-rendered every minute so the relative part stays true.
                TimelineView(.everyMinute) { _ in
                    Text(verbatim: "Synced \(TimeFormatting.refreshTimestamp(lastSyncAt))")
                        .foregroundStyle(.secondary)
                }
            } else {
                Text("Not synced yet")
                    .foregroundStyle(.secondary)
            }
        }
    }

    // MARK: - Listening

    @ViewBuilder
    private func timeSavedSection(settings: AppSettings) -> some View {
        let played = settings.lifetimePlayedSeconds
        let skipped = settings.lifetimeAdSkipSeconds
        let total = played + skipped
        let adPercent = total > 0 ? skipped / total * 100 : 0
        Section("Listening") {
            LabeledContent("Played (incl. ads)") {
                Text(TimeFormatting.minutesDuration(total))
                    .monospacedDigit()
                    .foregroundStyle(.secondary)
            }
            LabeledContent("Ads skipped") {
                Text(TimeFormatting.minutesDuration(skipped))
                    .monospacedDigit()
                    .foregroundStyle(.secondary)
            }
            LabeledContent("Ads") {
                Text(String(format: "%.1f%%", adPercent))
                    .monospacedDigit()
                    .foregroundStyle(.secondary)
            }
        }
    }

    @ViewBuilder
    private var usageHistorySection: some View {
        Section("History") {
            NavigationLink {
                UsageHistoryView()
            } label: {
                Label("Usage History", systemImage: "chart.bar.xaxis")
            }
        }
    }

    // MARK: - Playback

    @ViewBuilder
    private func playbackSection(settings: AppSettings) -> some View {
        @Bindable var s = settings
        Section("Playback") {
            Picker("Default playback speed", selection: $s.defaultPlaybackSpeed) {
                ForEach(PlaybackSpeed.options, id: \.self) { rate in
                    Text(PlaybackSpeed.label(for: rate)).tag(rate)
                }
            }
            .pickerStyle(.menu)
            Toggle("Auto-delete after fully played", isOn: $s.autoDeleteAfterPlayed)
        }
    }

    @ViewBuilder
    private func skippingSection(settings: AppSettings) -> some View {
        @Bindable var s = settings
        Section {
            Toggle("Skip ads", isOn: Binding(
                get: { settings.skipAds },
                set: { enabled in
                    settings.skipAds = enabled
                    PlayerService.shared.setSkipAdsEnabled(enabled)
                }
            ))
            Toggle("Skip intros & outros", isOn: $s.skipIntrosAndOutros)
            Stepper(value: $s.chainSkipGapSeconds, in: 0...30) {
                LabeledContent("Chain-skip gap") {
                    Text("\(s.chainSkipGapSeconds) s")
                        .monospacedDigit()
                        .foregroundStyle(.secondary)
                }
            }
        } header: {
            Text("Skipping")
        } footer: {
            Text("Detected segments are still marked on the timeline. After skipping a segment, the player peeks ahead by the chain-skip gap for another nearby segment and jumps that too. Set to 0 to skip only the current segment.")
        }
    }

    // MARK: - Streaming

    @ViewBuilder
    private var streamingSection: some View {
        Section {
            Picker("Stream episodes that aren't downloaded", selection: $streamingPolicy) {
                ForEach(StreamingPolicy.allCases, id: \.self) { policy in
                    Text(policy.label).tag(policy)
                }
            }
        } header: {
            Text("Streaming")
        } footer: {
            Text("Downloaded episodes always play from the device. This only decides whether other episodes may stream from your server instead.")
        }
    }

    // MARK: - Downloads & ad detection

    @ViewBuilder
    private func downloadsSection(settings: AppSettings) -> some View {
        Section {
            Picker("Auto-download", selection: Binding(
                get: { settings.autoDownloadPolicy },
                set: { settings.autoDownloadPolicy = $0 }
            )) {
                ForEach(AutoDownloadPolicy.allCases, id: \.self) { p in
                    Text(p.label).tag(p)
                }
            }
        } header: {
            Text("Downloads")
        }
    }

    @ViewBuilder
    private func adAnalysisSection(settings: AppSettings) -> some View {
        let sync = SyncService.shared
        let selectedModel = sync.pendingClassifierModel?.rawValue
            ?? settings.serverClassifierModel ?? ClassifierModel.defaultValue.rawValue
        Section {
            // Server mirror: SyncService applies the change optimistically
            // and rolls it back if the server refuses.
            Toggle("Detect & skip ads", isOn: Binding(
                get: { pendingAdAnalysis ?? settings.adAnalysisEnabled },
                set: { enabled in updateGlobalAdAnalysis(enabled) }
            ))
            .disabled(!isConfigured)
            Picker("Model", selection: Binding(
                get: { selectedModel },
                set: { updateClassifierModel($0) }
            )) {
                ForEach(ClassifierModel.allCases) { model in
                    Text(model.label).tag(model.rawValue)
                }
                if ClassifierModel(rawValue: selectedModel) == nil {
                    Text(selectedModel).tag(selectedModel)
                }
            }
            .pickerStyle(.menu)
            .accessibilityIdentifier("adAnalysisModelPicker")
            .disabled(!isConfigured || sync.pendingClassifierModel != nil || settings.serverOpenRouterAvailable == false)
            if sync.pendingClassifierModel != nil {
                HStack(spacing: 8) {
                    ProgressView()
                    Text("Saving model…").foregroundStyle(.secondary)
                }
            }
        } header: {
            Text("Ad analysis")
        } footer: {
            VStack(alignment: .leading, spacing: 4) {
                Text("Ad detection runs on your server, not on this device. When this is off, the server analyzes no podcast; when it's on, each podcast's own Detect & skip ads toggle still applies.")
                if settings.serverOpenRouterAvailable == false {
                    Text("Add an OpenRouter API key on your server to choose an analysis model.")
                }
                if !isConfigured {
                    Text("Connect to your server to change this.")
                }
            }
        }
    }

    private func updateClassifierModel(_ rawValue: String) {
        guard let model = ClassifierModel(rawValue: rawValue),
              rawValue != settings?.serverClassifierModel else { return }
        Task {
            do {
                try await SyncService.shared.setClassifierModel(model)
            } catch {
                showAlert(title: "Couldn't Change Analysis Model", message: error.localizedDescription)
            }
        }
    }

    private func updateGlobalAdAnalysis(_ enabled: Bool) {
        let request = adAnalysisRequestCount + 1
        adAnalysisRequestCount = request
        pendingAdAnalysis = enabled
        Task {
            do {
                try await SyncService.shared.setGlobalAdAnalysis(enabled)
            } catch {
                // SyncService has already rolled the mirror back.
                showAlert(title: "Couldn't Change Ad Detection", message: error.localizedDescription)
            }
            if adAnalysisRequestCount == request {
                pendingAdAnalysis = nil
            }
        }
    }

    // MARK: - Import

    @ViewBuilder
    private var importSection: some View {
        Section {
            Button {
                showOPMLPicker = true
            } label: {
                HStack {
                    Label("Import OPML", systemImage: "square.and.arrow.down")
                    if isImportingOPML {
                        Spacer()
                        ProgressView()
                    }
                }
            }
            .disabled(!isConfigured || isImportingOPML)
        } header: {
            Text("Import")
        } footer: {
            if !isConfigured {
                Text("Connect to your server to import subscriptions.")
            }
        }
    }

    private func handleOPMLImport(result: Result<URL, Error>) async {
        switch result {
        case .failure(let error):
            showAlert(title: "Import", message: error.localizedDescription)
        case .success(let url):
            // `importOPML` opens the security-scoped resource itself.
            isImportingOPML = true
            defer { isImportingOPML = false }
            do {
                let summary = try await SubscriptionService.shared.importOPML(from: url, in: context)
                showAlert(title: "Import", message: summary.message)
            } catch {
                showAlert(title: "Import", message: error.localizedDescription)
            }
        }
    }

    private func showAlert(title: String, message: String) {
        alertTitle = title
        alertMessage = message
    }
}
