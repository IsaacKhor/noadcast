import SwiftUI

/// Server address and access token, a connection test that saves nothing,
/// and the sync maintenance actions.
struct ServerSetupView: View {
    @State private var addressText = ""
    @State private var tokenText = ""
    @State private var hasLoadedFields = false
    @State private var isTesting = false
    @State private var testResult: ConnectionTestResult?
    @State private var saveStatus: SaveStatus?
    @State private var showResyncConfirmation = false
    @State private var showResetCacheConfirmation = false
    @State private var showSignOutConfirmation = false

    private enum ConnectionTestResult {
        case invalidAddress
        case unreachable(address: String, message: String)
        case reached(address: String, health: HealthDTO, token: TokenCheck)
    }

    private enum TokenCheck {
        case accepted
        case notRequired
        case failed(String)
    }

    private enum SaveStatus {
        case saved
        case failed(String)
    }

    private var trimmedAddress: String {
        addressText.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    /// `nil` when blank: a server started without auth needs no token.
    private var trimmedToken: String? {
        let trimmed = tokenText.trimmingCharacters(in: .whitespacesAndNewlines)
        return trimmed.isEmpty ? nil : trimmed
    }

    var body: some View {
        Form {
            addressSection
            testSection
            saveSection
            if APIConfiguration.isConfigured {
                syncSection
            }
            if let migrationStatus = DeviceStateRestoreService.shared.statusLine {
                Section("Migration") {
                    Text(migrationStatus)
                        .foregroundStyle(.secondary)
                }
            }
        }
        .navigationTitle("Server")
        .navigationBarTitleDisplayMode(.inline)
        .scrollDismissesKeyboard(.interactively)
        .onAppear { loadSavedConfiguration() }
        // Results describe the fields as they were; drop them on any edit.
        .onChange(of: addressText) { clearFeedback() }
        .onChange(of: tokenText) { clearFeedback() }
    }

    // MARK: - Address

    private var addressSection: some View {
        Section {
            TextField("Server address", text: $addressText, prompt: Text(APIConfiguration.placeholderBaseURL))
                .keyboardType(.URL)
                .textContentType(.URL)
                .textInputAutocapitalization(.never)
                .autocorrectionDisabled()
            SecureField("Access token", text: $tokenText)
                .textContentType(.password)
                .textInputAutocapitalization(.never)
                .autocorrectionDisabled()
        } header: {
            Text("Address")
        } footer: {
            Text("Use your server's Tailscale name, e.g. laurel.turkey-galaxy.ts.net:8765. Tailnet addresses don't need iOS's local-network permission — which the audio player can't request, so streaming from a LAN address (.local or 192.168.x.x) fails silently until it's granted. Test connection makes a plain request, which triggers that permission prompt for LAN users.")
        }
    }

    // MARK: - Test

    private var testSection: some View {
        Section {
            Button {
                Task { await testConnection() }
            } label: {
                HStack {
                    Text("Test connection")
                    Spacer()
                    if isTesting {
                        ProgressView()
                    }
                }
            }
            .disabled(isTesting || trimmedAddress.isEmpty)
            if let testResult {
                testResultRows(testResult)
            }
        }
    }

    @ViewBuilder
    private func testResultRows(_ result: ConnectionTestResult) -> some View {
        switch result {
        case .invalidAddress:
            Label("Not a valid http(s) address", systemImage: "xmark.octagon.fill")
                .foregroundStyle(.red)
        case .unreachable(let address, let message):
            Text(verbatim: address)
                .font(.caption)
                .foregroundStyle(.secondary)
            failureLabel(message)
        case .reached(let address, let health, let token):
            Text(verbatim: address)
                .font(.caption)
                .foregroundStyle(.secondary)
            LabeledContent("Server version", value: Self.versionText(for: health))
            LabeledContent("Instance", value: Self.instanceText(for: health))
            VStack(alignment: .leading, spacing: 4) {
                Text("Capabilities")
                Text(verbatim: Self.capabilitiesText(for: health))
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            if health.status.lowercased() != "ok" {
                LabeledContent("Status", value: health.status)
            }
            switch token {
            case .accepted:
                Label("Token accepted", systemImage: "checkmark.circle.fill")
                    .foregroundStyle(.green)
            case .notRequired:
                Label("This server doesn't require a token", systemImage: "checkmark.circle")
                    .foregroundStyle(.green)
            case .failed(let message):
                failureLabel(message)
            }
        }
    }

    /// Error text is shown verbatim: it can contain `_` or `*`, which a
    /// `LocalizedStringKey` would parse as Markdown.
    private func failureLabel(_ message: String) -> some View {
        Label {
            Text(verbatim: message)
        } icon: {
            Image(systemName: "xmark.octagon.fill")
        }
        .foregroundStyle(.red)
    }

    private static func versionText(for health: HealthDTO) -> String {
        let version = health.version ?? "Unknown"
        guard let apiVersion = health.apiVersion else { return version }
        return "\(version) (API v\(apiVersion))"
    }

    private static func instanceText(for health: HealthDTO) -> String {
        guard let instanceId = health.instanceId, !instanceId.isEmpty else { return "Not reported" }
        return String(instanceId.prefix(8))
    }

    private static func capabilitiesText(for health: HealthDTO) -> String {
        health.capabilities.isEmpty ? "None reported" : health.capabilities.joined(separator: ", ")
    }

    /// `GET /health`, then `GET /api/v1/session`, against the address and
    /// token as typed. Uses a throwaway client: nothing is saved, and a 401
    /// here must not flip the app-wide auth banner.
    private func testConnection() async {
        guard let url = APIConfiguration.normalizedBaseURL(from: addressText) else {
            testResult = .invalidAddress
            return
        }
        let endpoint = APIConfiguration.Endpoint(baseURL: url, token: trimmedToken)
        let hasToken = endpoint.token != nil
        let address = url.absoluteString
        let urlSession = NoadcastAPIClient.makeDefaultSession()
        let client = NoadcastAPIClient(
            session: urlSession,
            endpointProvider: { endpoint },
            retryPolicy: .once,
            authStateHandler: nil
        )
        isTesting = true
        testResult = nil
        defer {
            isTesting = false
            urlSession.finishTasksAndInvalidate()
        }

        let health: HealthDTO
        do {
            health = try await client.health()
        } catch {
            testResult = .unreachable(address: address, message: error.localizedDescription)
            return
        }

        let tokenCheck: TokenCheck
        do {
            let session = try await client.sessionInfo()
            if !health.authRequired {
                tokenCheck = .notRequired
            } else if session.authenticated {
                tokenCheck = .accepted
            } else {
                tokenCheck = .failed(APIError.unauthorized.localizedDescription)
            }
        } catch APIError.unauthorized {
            tokenCheck = .failed(hasToken
                ? APIError.unauthorized.localizedDescription
                : "This server requires an access token.")
        } catch {
            tokenCheck = .failed(error.localizedDescription)
        }
        testResult = .reached(address: address, health: health, token: tokenCheck)
    }

    // MARK: - Save

    private var saveSection: some View {
        Section {
            Button("Save") { save() }
                .disabled(trimmedAddress.isEmpty)
            if let saveStatus {
                switch saveStatus {
                case .saved:
                    Label("Saved", systemImage: "checkmark.circle.fill")
                        .foregroundStyle(.green)
                case .failed(let message):
                    failureLabel(message)
                }
            }
        } footer: {
            Text("Saving replaces the address and token on this device and syncs right away. Testing doesn't save anything.")
        }
    }

    private func save() {
        guard APIConfiguration.save(baseURLString: addressText, token: trimmedToken) else {
            saveStatus = .failed("Not a valid http(s) address")
            return
        }
        saveStatus = .saved
        Task { await SyncService.shared.serverConfigurationDidChange() }
    }

    // MARK: - Sync

    @ViewBuilder
    private var syncSection: some View {
        let sync = SyncService.shared
        Section {
            LabeledContent("Last sync") {
                if let lastSyncAt = sync.lastSyncAt {
                    // Re-rendered every minute so the relative part stays true.
                    TimelineView(.everyMinute) { _ in
                        Text(verbatim: TimeFormatting.refreshTimestamp(lastSyncAt))
                    }
                } else {
                    Text("Never")
                }
            }
            if let instanceId = sync.instanceId, !instanceId.isEmpty {
                LabeledContent("Server instance", value: String(instanceId.prefix(8)))
            }
            if sync.isSyncing {
                HStack(spacing: 8) {
                    ProgressView()
                    Text("Syncing…")
                        .foregroundStyle(.secondary)
                }
            }
            if sync.authFailed {
                Label("Token rejected. Enter the current token and tap Save.", systemImage: "exclamationmark.triangle.fill")
                    .foregroundStyle(.red)
            }
            if let lastError = sync.lastError {
                Text(verbatim: lastError)
                    .foregroundStyle(.red)
            }
            Button("Sync now") {
                Task { await SyncService.shared.syncNow(.manual) }
            }
            .disabled(sync.isSyncing)
            Button("Re-sync from scratch") {
                showResyncConfirmation = true
            }
            .confirmationDialog(
                "Re-sync from scratch?",
                isPresented: $showResyncConfirmation,
                titleVisibility: .visible
            ) {
                Button("Re-sync") {
                    Task { await SyncService.shared.resyncFromScratch() }
                }
                Button("Cancel", role: .cancel) {}
            } message: {
                Text("Fetches your whole library from the server again and removes podcasts and episodes it no longer has. Everything else on this device, including downloads and playback positions, is kept.")
            }
            Button("Reset local cache", role: .destructive) {
                showResetCacheConfirmation = true
            }
            .confirmationDialog(
                "Reset local cache?",
                isPresented: $showResetCacheConfirmation,
                titleVisibility: .visible
            ) {
                Button("Reset Local Cache", role: .destructive) {
                    Task { await SyncService.shared.resetLocalCache() }
                }
                Button("Cancel", role: .cancel) {}
            } message: {
                Text("Deletes every downloaded episode from this device and rebuilds the library from your server. Playback positions, played flags and the queue are kept.")
            }
            Button("Sign out", role: .destructive) {
                showSignOutConfirmation = true
            }
            .confirmationDialog(
                "Sign out?",
                isPresented: $showSignOutConfirmation,
                titleVisibility: .visible
            ) {
                Button("Sign Out", role: .destructive) {
                    signOut()
                }
                Button("Cancel", role: .cancel) {}
            } message: {
                Text("Forgets the access token on this device. Downloaded episodes stay playable, and the server address is kept so you can sign in again with a token.")
            }
        } header: {
            Text("Sync")
        } footer: {
            Text("Re-sync fetches the whole library again. Resetting the local cache also deletes downloaded audio, but keeps playback positions, played flags and the queue.")
        }
    }

    // MARK: - Actions

    /// Once per view lifetime, so returning to the tab doesn't overwrite
    /// unsaved edits.
    private func loadSavedConfiguration() {
        guard !hasLoadedFields else { return }
        hasLoadedFields = true
        addressText = APIConfiguration.baseURLString ?? ""
        tokenText = APIConfiguration.token ?? ""
    }

    private func clearFeedback() {
        testResult = nil
        saveStatus = nil
    }

    private func signOut() {
        SyncService.shared.signOut(forgetServerAddress: false)
        tokenText = ""
    }
}
