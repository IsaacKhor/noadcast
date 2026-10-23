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
    @State private var feedIntervalDraft: String?
    @State private var feedIntervalSaveFailed = false
    @State private var serverSettingsLoadError: String?
    @FocusState private var feedIntervalFocused: Bool

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
                    feedUpdatesSection(settings: s)
                    adAnalysisSection(settings: s)
                    timeSavedSection(settings: s)
                    usageHistorySection
                    playbackSection(settings: s)
                    skippingSection(settings: s)
                }
                streamingSection
                if let s = settings {
                    downloadsSection(settings: s)
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
            .task {
                await refreshServerSettings()
            }
            .onChange(of: storedServerAddress) { _, _ in
                feedIntervalDraft = nil
                feedIntervalSaveFailed = false
                serverSettingsLoadError = nil
            }
            .onChange(of: SyncService.shared.serverConfigurationRevision) { _, _ in
                feedIntervalDraft = nil
                feedIntervalSaveFailed = false
                Task { await refreshServerSettings() }
            }
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
            if isConfigured, let serverSettingsLoadError {
                Label {
                    Text(verbatim: "Settings unavailable: \(serverSettingsLoadError)")
                } icon: {
                    Image(systemName: "exclamationmark.circle")
                }
                .foregroundStyle(.red)
                Button("Retry loading settings") {
                    Task { await refreshServerSettings() }
                }
            } else if isConfigured && !SyncService.shared.hasLoadedServerSettings {
                HStack(spacing: 8) {
                    ProgressView()
                    Text("Loading server settings…")
                        .foregroundStyle(.secondary)
                }
            }
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

    // MARK: - Server settings

    private func refreshServerSettings() async {
        guard isConfigured else { return }
        let requestedServer = serverURL
        let requestedRevision = SyncService.shared.serverConfigurationRevision
        serverSettingsLoadError = nil
        do {
            try await SyncService.shared.refreshServerSettings()
        } catch {
            if serverURL == requestedServer
                && SyncService.shared.serverConfigurationRevision == requestedRevision {
                serverSettingsLoadError = error.localizedDescription
            }
        }
    }

    private func feedUpdatesSection(settings: AppSettings) -> some View {
        let sync = SyncService.shared
        let current = sync.hasLoadedServerSettings ? settings.serverFeedIntervalMinutes : nil
        let input = feedIntervalDraft ?? current.map(String.init) ?? ""
        let parsed = FeedIntervalMinutes.parse(input)
        let canEdit = isConfigured && sync.hasLoadedServerSettings
            && !sync.isUpdatingServerSettings && current != nil
        return Section {
            HStack {
                Text("Check every")
                Spacer()
                TextField("Minutes", text: Binding(
                    get: { feedIntervalDraft ?? settings.serverFeedIntervalMinutes.map(String.init) ?? "" },
                    set: {
                        feedIntervalDraft = $0
                        feedIntervalSaveFailed = false
                    }
                ))
                .keyboardType(.numberPad)
                .multilineTextAlignment(.trailing)
                .frame(width: 68)
                .focused($feedIntervalFocused)
                .accessibilityIdentifier("feedIntervalMinutesField")
                Text("minutes")
                    .foregroundStyle(.secondary)
            }
            .disabled(!canEdit)
            if current == nil && sync.hasLoadedServerSettings {
                Text("Feed refresh interval is unavailable on this server.")
                    .foregroundStyle(.secondary)
            } else if canEdit && feedIntervalDraft != nil && parsed == nil {
                Text("Enter a whole number from 1 to 1,440.")
                    .foregroundStyle(.red)
            } else if feedIntervalSaveFailed {
                Text("Not saved. Try again.")
                    .foregroundStyle(.red)
            } else if canEdit && parsed != current {
                Text("Unsaved change")
                    .foregroundStyle(.secondary)
            }
            Button("Save refresh interval") {
                feedIntervalFocused = false
                guard let parsed else { return }
                Task { await saveFeedInterval(parsed) }
            }
            .disabled(!canEdit || parsed == nil || parsed == current)
            .accessibilityIdentifier("saveFeedIntervalButton")
            if sync.isUpdatingServerSettings {
                HStack(spacing: 8) {
                    ProgressView()
                    Text("Saving server settings…")
                        .foregroundStyle(.secondary)
                }
            }
        } header: {
            Text("Feed updates")
        } footer: {
            Text("Your server checks each feed at this interval. Choose 1 to 1,440 minutes.")
        }
    }

    private func saveFeedInterval(_ minutes: Int) async {
        do {
            try await SyncService.shared.setFeedIntervalMinutes(minutes)
            feedIntervalDraft = nil
            feedIntervalSaveFailed = false
        } catch {
            feedIntervalSaveFailed = true
            showAlert(title: "Couldn't Save Refresh Interval", message: error.localizedDescription)
        }
    }

    // MARK: - Downloads

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
        let selectedModel = sync.hasLoadedServerSettings ? (settings.serverClassifierModel ?? "") : ""
        let canEdit = isConfigured && sync.hasLoadedServerSettings && !sync.isUpdatingServerSettings
        let canChooseModel = settings.serverOpenRouterAvailable != false
        Section {
            if sync.hasLoadedServerSettings {
                Toggle("Analyze ads", isOn: Binding(
                    get: { settings.adAnalysisEnabled },
                    set: { updateGlobalAdAnalysis($0) }
                ))
                .disabled(!canEdit)
                .accessibilityIdentifier("globalAdAnalysisToggle")
                Picker("Model", selection: Binding(
                    get: { selectedModel },
                    set: { updateClassifierModel($0) }
                )) {
                    ForEach(ClassifierModel.allCases) { model in
                        Text(model.label).tag(model.rawValue)
                    }
                    if ClassifierModel(rawValue: selectedModel) == nil {
                        Text(selectedModel.isEmpty ? "Unavailable" : selectedModel).tag(selectedModel)
                    }
                }
                .pickerStyle(.menu)
                .accessibilityIdentifier("adAnalysisModelPicker")
                .disabled(!canEdit || !canChooseModel)
                Toggle("OpenRouter Flex", isOn: Binding(
                    get: { settings.serverOpenRouterFlexEnabled ?? false },
                    set: { updateOpenRouterFlex($0) }
                ))
                .disabled(!canEdit || settings.serverOpenRouterFlexEnabled == nil)
                .accessibilityIdentifier("openRouterFlexToggle")
            } else {
                Text("Load server settings to change ad analysis.")
                    .foregroundStyle(.secondary)
            }
        } header: {
            Text("Ad analysis")
        } footer: {
            VStack(alignment: .leading, spacing: 4) {
                Text("Analysis applies to future episodes. Flex may take longer or be less available; any savings depend on the selected model's endpoints. Standard pricing applies where Flex is unavailable.")
                if settings.serverOpenRouterAvailable == false {
                    Text("Add an OpenRouter API key on your server to run analysis or choose a model.")
                }
                if settings.serverOpenRouterFlexEnabled == nil && sync.hasLoadedServerSettings {
                    Text("Flex is unavailable on this server.")
                }
                if !isConfigured {
                    Text("Connect to your server to change this.")
                }
            }
        }
    }

    private func updateGlobalAdAnalysis(_ enabled: Bool) {
        guard enabled != settings?.adAnalysisEnabled else { return }
        Task {
            do {
                try await SyncService.shared.setGlobalAdAnalysisEnabled(enabled)
            } catch {
                showAlert(title: "Couldn't Change Ad Analysis", message: error.localizedDescription)
            }
        }
    }

    private func updateOpenRouterFlex(_ enabled: Bool) {
        guard enabled != settings?.serverOpenRouterFlexEnabled else { return }
        Task {
            do {
                try await SyncService.shared.setOpenRouterFlexEnabled(enabled)
            } catch {
                showAlert(title: "Couldn't Change Flex", message: error.localizedDescription)
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
