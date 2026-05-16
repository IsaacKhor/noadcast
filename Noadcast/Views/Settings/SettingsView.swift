import SwiftUI
import SwiftData
import UniformTypeIdentifiers

struct SettingsView: View {
    @Environment(\.modelContext) private var context
    @Query private var settingsList: [AppSettings]

    @State private var showOPMLPicker = false
    @State private var opmlMessage: String?

    private var settings: AppSettings? { settingsList.first }

    var body: some View {
        NavigationStack {
            Form {
                if let s = settings {
                    timeSavedSection(settings: s)
                    playbackSection(settings: s)
                    downloadsSection(settings: s)
                    adProviderSection(settings: s)
                    adDetectionSection(settings: s)
                    importSection
                }
            }
            .navigationTitle("Settings")
            .navigationBarTitleDisplayMode(.inline)
            .fileImporter(
                isPresented: $showOPMLPicker,
                allowedContentTypes: [.xml, UTType(filenameExtension: "opml") ?? .xml]
            ) { result in
                Task { await handleOPMLImport(result: result) }
            }
            .alert("Import", isPresented: .constant(opmlMessage != nil), actions: {
                Button("OK") { opmlMessage = nil }
            }, message: {
                Text(opmlMessage ?? "")
            })
        }
    }

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
    private func downloadsSection(settings: AppSettings) -> some View {
        @Bindable var s = settings
        Section("Downloads") {
            Picker("Auto-download", selection: Binding(
                get: { s.autoDownloadPolicy },
                set: { s.autoDownloadPolicy = $0 }
            )) {
                ForEach(AutoDownloadPolicy.allCases, id: \.self) { p in
                    Text(p.label).tag(p)
                }
            }
        }
    }

    @ViewBuilder
    private func adProviderSection(settings: AppSettings) -> some View {
        @Bindable var s = settings
        let provider = s.adDetectionProvider
        Section {
            Picker("Model", selection: Binding(
                get: { s.adDetectionProvider },
                set: { s.adDetectionProvider = $0 }
            )) {
                ForEach(AdDetectionProvider.allCases, id: \.self) { p in
                    Text(p.label).tag(p)
                }
            }
            if provider.requiresGoogleKey {
                SecureField(
                    "Google AI API key",
                    text: Binding(
                        get: { s.googleAPIKey ?? "" },
                        set: { s.googleAPIKey = $0.isEmpty ? nil : $0 }
                    )
                )
                .textContentType(.password)
                .autocorrectionDisabled()
                .textInputAutocapitalization(.never)
            }
            if provider.requiresOpenAIKey {
                SecureField(
                    "OpenAI API key",
                    text: Binding(
                        get: { s.openAIAPIKey ?? "" },
                        set: { s.openAIAPIKey = $0.isEmpty ? nil : $0 }
                    )
                )
                .textContentType(.password)
                .autocorrectionDisabled()
                .textInputAutocapitalization(.never)
            }
        } header: {
            Text("Detection model")
        } footer: {
            Text(providerFooter(for: provider))
        }
    }

    private func providerFooter(for provider: AdDetectionProvider) -> String {
        switch provider {
        case .geminiFlashLite:
            "Sends the full transcript to Google in a single request and parses a structured JSON response. Get an API key at aistudio.google.com."
        case .gpt54Nano, .gpt54Mini:
            "Sends the full transcript to OpenAI in a single request and parses a structured JSON response. Get an API key at platform.openai.com."
        }
    }

    @ViewBuilder
    private func adDetectionSection(settings: AppSettings) -> some View {
        Section("Ad detection") {
            NavigationLink {
                AdPromptView()
            } label: {
                Label("Detection prompt", systemImage: "text.bubble")
            }
        }
    }

    @ViewBuilder

    private var importSection: some View {
        Section("Import") {
            Button {
                showOPMLPicker = true
            } label: {
                Label("Import OPML", systemImage: "square.and.arrow.down")
            }
        }
    }

    private static func opmlSummary(added: Int, skipped: Int, failed: Int) -> String {
        if added == 0 && skipped == 0 && failed == 0 {
            return "No podcast feeds were found in the OPML file."
        }
        var parts: [String] = []
        parts.append("Added \(added).")
        if skipped > 0 {
            parts.append("Skipped \(skipped) already subscribed.")
        }
        if failed > 0 {
            parts.append("\(failed) couldn't be fetched.")
        }
        return parts.joined(separator: " ")
    }

    private func handleOPMLImport(result: Result<URL, Error>) async {
        switch result {
        case .failure(let err):
            opmlMessage = err.localizedDescription
        case .success(let url):
            let didStart = url.startAccessingSecurityScopedResource()
            defer { if didStart { url.stopAccessingSecurityScopedResource() } }
            do {
                let entries = try await OPMLService.shared.parse(url: url)
                var added = 0
                var skipped = 0
                var failed = 0
                for entry in entries {
                    let feedURL = entry.feedURL
                    let existing = (try? context.fetch(
                        FetchDescriptor<Podcast>(predicate: #Predicate { $0.feedURL == feedURL })
                    ))?.first
                    if existing != nil {
                        skipped += 1
                        continue
                    }
                    do {
                        _ = try await SubscriptionService.shared.subscribe(feedURL: feedURL, in: context)
                        added += 1
                    } catch {
                        failed += 1
                    }
                }
                opmlMessage = Self.opmlSummary(added: added, skipped: skipped, failed: failed)
            } catch {
                opmlMessage = error.localizedDescription
            }
        }
    }
}
