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
                    adDetectionSection(settings: s)
                    adFilterSection(settings: s)
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
        Section("Time saved") {
            LabeledContent("Skipped ads") {
                Text(TimeFormatting.longDuration(settings.lifetimeAdSkipSeconds))
                    .monospacedDigit()
                    .foregroundStyle(.secondary)
            }
            LabeledContent("Faster playback") {
                Text(TimeFormatting.longDuration(settings.lifetimeSpeedupSeconds))
                    .monospacedDigit()
                    .foregroundStyle(.secondary)
            }
            LabeledContent("Total") {
                Text(TimeFormatting.longDuration(
                    settings.lifetimeAdSkipSeconds + settings.lifetimeSpeedupSeconds
                ))
                .monospacedDigit()
                .bold()
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
    private func adDetectionSection(settings: AppSettings) -> some View {
        Section("Ad detection") {
            NavigationLink {
                AdPromptView(settings: settings)
            } label: {
                Label("Detection prompt", systemImage: "text.bubble")
            }
        }
    }

    @ViewBuilder
    private func adFilterSection(settings: AppSettings) -> some View {
        @Bindable var s = settings
        Section {
            Stepper(value: $s.adMergeGapSeconds, in: 0...60) {
                LabeledContent("Merge gap") {
                    Text("\(s.adMergeGapSeconds) s")
                        .monospacedDigit()
                        .foregroundStyle(.secondary)
                }
            }
            Stepper(value: $s.adMinDurationSeconds, in: 0...120) {
                LabeledContent("Min ad length") {
                    Text("\(s.adMinDurationSeconds) s")
                        .monospacedDigit()
                        .foregroundStyle(.secondary)
                }
            }
        } header: {
            Text("Ad filtering")
        } footer: {
            Text("Adjacent ads less than \(s.adMergeGapSeconds) s apart are merged into one. Detected ads shorter than \(s.adMinDurationSeconds) s are discarded as likely false positives. Re-run ad detection on existing episodes from the Downloads tab to apply changes.")
        }
    }

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
