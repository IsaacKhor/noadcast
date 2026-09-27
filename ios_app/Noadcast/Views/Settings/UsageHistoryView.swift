import SwiftUI
import SwiftData
import Charts

struct UsageHistoryView: View {
    @Environment(\.modelContext) private var context
    @Query private var settingsList: [AppSettings]
    @Query(sort: \UsageHistoryDay.dayStart) private var playbackDays: [UsageHistoryDay]

    @State private var showResetPlaybackHistoryConfirmation = false
    /// `GET /api/v1/usage?days=30`. Stays `nil`, hiding the server
    /// sections, when no server is configured, the server predates the
    /// endpoint (404), or the request fails.
    @State private var serverUsage: UsageDTO?

    private var visibleDayStarts: [Date] {
        let playbackStarts = playbackDays
            .filter(\.hasPlayback)
            .map(\.dayStart)
        return Array(Set(playbackStarts).sorted().suffix(30))
    }

    private var playbackDaysByStart: [Date: UsageHistoryDay] {
        playbackDays.reduce(into: [:]) { result, day in
            result[day.dayStart] = day
        }
    }

    private var playbackRows: [HistoryChartRow] {
        let daysByStart = playbackDaysByStart
        return visibleDayStarts.compactMap { daysByStart[$0] }.flatMap { day in
            [
                HistoryChartRow(
                    day: day.dayStart,
                    category: "Played",
                    value: day.playbackSeconds / 60
                ),
                HistoryChartRow(
                    day: day.dayStart,
                    category: "Skipped",
                    value: day.adSkippedSeconds / 60
                )
            ]
        }.filter { $0.value > 0 }
    }

    private var totalPlaybackSeconds: Double {
        let daysByStart = playbackDaysByStart
        return visibleDayStarts
            .compactMap { daysByStart[$0] }
            .reduce(0) { $0 + $1.totalPlaybackSeconds }
    }

    private var hasPlaybackHistory: Bool {
        playbackDays.contains(where: \.hasPlayback)
            || settingsList.contains {
                $0.lifetimePlayedSeconds > 0 || $0.lifetimeAdSkipSeconds > 0
            }
    }

    var body: some View {
        Form {
            if visibleDayStarts.isEmpty {
                ContentUnavailableView(
                    "No Playback Yet",
                    systemImage: "chart.bar.xaxis",
                    description: Text("Daily playback totals will appear here after you listen to an episode.")
                )
            } else {
                summarySection
                playbackSection
            }
            if let serverUsage {
                serverTotalsSection(serverUsage)
                serverTokensSection(serverUsage)
                serverModelsSection(serverUsage)
            }
            playbackHistoryActionsSection
        }
        .navigationTitle("Usage History")
        .navigationBarTitleDisplayMode(.inline)
        .task {
            if APIConfiguration.isConfigured {
                serverUsage = try? await NoadcastAPIClient.shared.usage(days: 30)
            }
        }
        .confirmationDialog(
            "Reset playback history?",
            isPresented: $showResetPlaybackHistoryConfirmation,
            titleVisibility: .visible
        ) {
            Button("Reset Playback History", role: .destructive) {
                resetPlaybackHistory()
            }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text("This clears daily playback totals and listening time saved statistics.")
        }
    }

    // MARK: - Local playback

    private var summarySection: some View {
        Section("Last 30 Days") {
            LabeledContent("Playback") {
                Text(TimeFormatting.minutesDuration(totalPlaybackSeconds))
                    .foregroundStyle(.secondary)
                    .monospacedDigit()
            }
        }
    }

    private var playbackSection: some View {
        Section("Playback Per Day") {
            if playbackRows.isEmpty {
                Text("No playback history yet.")
                    .foregroundStyle(.secondary)
            } else {
                Chart(playbackRows) { row in
                    BarMark(
                        x: .value("Day", row.day, unit: .day),
                        y: .value("Minutes", row.value)
                    )
                    .foregroundStyle(by: .value("Type", row.category))
                }
                .chartXAxis {
                    AxisMarks(values: .automatic(desiredCount: 5)) {
                        AxisGridLine()
                        AxisTick()
                        AxisValueLabel(format: .dateTime.month(.abbreviated).day())
                    }
                }
                .chartYAxisLabel("Minutes")
                .chartLegend(position: .bottom)
                .frame(height: 220)
            }
        }
    }

    private var playbackHistoryActionsSection: some View {
        Section("Playback History") {
            Button(role: .destructive) {
                showResetPlaybackHistoryConfirmation = true
            } label: {
                Label("Reset Playback History", systemImage: "arrow.counterclockwise")
            }
            .disabled(!hasPlaybackHistory)
        }
    }

    // MARK: - Server ad-detection usage

    @ViewBuilder
    private func serverTotalsSection(_ usage: UsageDTO) -> some View {
        let totals = Self.serverTotals(for: usage)
        Section("Server Usage · Last 30 Days") {
            LabeledContent("Detection calls") {
                Text(totals.calls.formatted())
                    .foregroundStyle(.secondary)
                    .monospacedDigit()
            }
            LabeledContent("Tokens") {
                Text(formatTokens(totals.totalTokens))
                    .foregroundStyle(.secondary)
                    .monospacedDigit()
            }
            LabeledContent("Estimated cost") {
                Text(formatCost(totals.costUsd))
                    .foregroundStyle(.secondary)
                    .monospacedDigit()
            }
        }
    }

    @ViewBuilder
    private func serverTokensSection(_ usage: UsageDTO) -> some View {
        let rows = Self.serverTokenRows(for: usage)
        Section {
            if rows.isEmpty {
                Text("No detection calls in the last 30 days.")
                    .foregroundStyle(.secondary)
            } else {
                Chart(rows) { row in
                    BarMark(
                        x: .value("Day", row.day, unit: .day),
                        y: .value("Tokens", row.value)
                    )
                    .foregroundStyle(by: .value("Type", row.category))
                }
                .chartXAxis {
                    AxisMarks(values: .automatic(desiredCount: 5)) {
                        AxisGridLine()
                        AxisTick()
                        AxisValueLabel(format: .dateTime.month(.abbreviated).day())
                    }
                }
                .chartYAxisLabel("Tokens")
                .chartLegend(position: .bottom)
                .frame(height: 220)
            }
        } header: {
            Text("Server Tokens Per Day")
        } footer: {
            Text("Thought tokens show reasoning usage when reported by OpenRouter.")
        }
    }

    @ViewBuilder
    private func serverModelsSection(_ usage: UsageDTO) -> some View {
        if !usage.byModel.isEmpty {
            Section("Server Usage By Model") {
                ForEach(usage.byModel) { model in
                    VStack(alignment: .leading, spacing: 4) {
                        HStack {
                            Text(verbatim: "\(model.provider) · \(model.model)")
                                .lineLimit(1)
                            Spacer()
                            Text(formatCost(model.costUsd))
                                .foregroundStyle(.secondary)
                                .monospacedDigit()
                        }
                        Text(verbatim: "\(Self.callsLabel(model.calls)) · \(formatTokens(model.totalTokens)) tokens")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                            .lineLimit(1)
                    }
                }
            }
        }
    }

    /// The server's `totals`, or the sum of its days when it omits them.
    private static func serverTotals(for usage: UsageDTO) -> UsageTotalsDTO {
        if let totals = usage.totals {
            return totals
        }
        return UsageTotalsDTO(
            calls: usage.days.reduce(0) { $0 + $1.calls },
            inputTokens: usage.days.reduce(0) { $0 + $1.inputTokens },
            thoughtTokens: usage.days.reduce(0) { $0 + $1.thoughtTokens },
            outputTokens: usage.days.reduce(0) { $0 + $1.outputTokens },
            costUsd: usage.days.reduce(0.0) { $0 + $1.costUsd }
        )
    }

    /// Stacked input/thought/output bars per server day. Days whose `date`
    /// doesn't parse are skipped. A category is included only if some day
    /// has it, and then for every day (zeros draw nothing), so the stacking
    /// order stays input, thought, output.
    private static func serverTokenRows(for usage: UsageDTO) -> [HistoryChartRow] {
        var dated: [(day: Date, entry: UsageDayDTO)] = []
        for entry in usage.days {
            guard let day = entry.day else { continue }
            dated.append((day: day, entry: entry))
        }
        dated.sort { $0.day < $1.day }
        let hasInput = dated.contains { $0.entry.inputTokens > 0 }
        let hasThought = dated.contains { $0.entry.thoughtTokens > 0 }
        let hasOutput = dated.contains { $0.entry.outputTokens > 0 }
        var rows: [HistoryChartRow] = []
        for item in dated {
            if hasInput {
                rows.append(HistoryChartRow(day: item.day, category: "Input", value: Double(item.entry.inputTokens)))
            }
            if hasThought {
                rows.append(HistoryChartRow(day: item.day, category: "Thought", value: Double(item.entry.thoughtTokens)))
            }
            if hasOutput {
                rows.append(HistoryChartRow(day: item.day, category: "Output", value: Double(item.entry.outputTokens)))
            }
        }
        return rows
    }

    private static func callsLabel(_ calls: Int) -> String {
        calls == 1 ? "1 call" : "\(calls.formatted()) calls"
    }

    private func formatTokens(_ count: Int) -> String {
        if count >= 1_000_000 {
            return String(format: "%.1fM", Double(count) / 1_000_000)
        }
        if count >= 1_000 {
            return String(format: "%.1fK", Double(count) / 1_000)
        }
        return count.formatted()
    }

    /// Costs are typically pennies; show fractions of a cent precisely
    /// rather than rounding to $0.00 and looking broken.
    private func formatCost(_ amount: Double) -> String {
        if amount >= 1 {
            return String(format: "$%.2f", amount)
        }
        if amount >= 0.01 {
            return String(format: "$%.3f", amount)
        }
        if amount > 0 {
            return String(format: "$%.4f", amount)
        }
        return "$0"
    }

    private func resetPlaybackHistory() {
        let settings = settingsList.first ?? AppSettings.current(in: context)
        PlayerService.shared.discardPendingPlaybackHistory()
        settings.resetPlaybackHistoryStatistics()
        UsageHistoryDay.resetAll(in: context)
        try? context.save()
    }
}

private struct HistoryChartRow: Identifiable {
    let id = UUID()
    let day: Date
    let category: String
    let value: Double
}
