import SwiftUI
import SwiftData
import Charts

struct UsageHistoryView: View {
    @Query(sort: \UsageHistoryDay.dayStart) private var playbackDays: [UsageHistoryDay]
    @Query(sort: \TokenUsageRecord.createdAt) private var tokenRecords: [TokenUsageRecord]

    private var visibleDayStarts: [Date] {
        let playbackStarts = playbackDays
            .filter(\.hasPlayback)
            .map(\.dayStart)
        let tokenStarts = tokenRecords.map { Calendar.current.startOfDay(for: $0.createdAt) }
        return Array(Set(playbackStarts + tokenStarts).sorted().suffix(30))
    }

    private var playbackDaysByStart: [Date: UsageHistoryDay] {
        playbackDays.reduce(into: [:]) { result, day in
            result[day.dayStart] = day
        }
    }

    private var tokenRecordsByDay: [Date: [TokenUsageRecord]] {
        Dictionary(grouping: tokenRecords) { record in
            Calendar.current.startOfDay(for: record.createdAt)
        }
    }

    private var playbackRows: [HistoryChartRow] {
        visibleDayStarts.compactMap { playbackDaysByStart[$0] }.flatMap { day in
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

    private var tokenDaySummaries: [TokenDaySummary] {
        visibleDayStarts.compactMap { day in
            let records = tokenRecordsByDay[day] ?? []
            let input = records.reduce(0) { $0 + $1.inputTokens }
            let thought = records.reduce(0) { $0 + $1.thoughtTokens }
            let output = records.reduce(0) { $0 + $1.outputTokens }
            let cost = records.reduce(0) { $0 + $1.totalCostUSD }
            guard input > 0 || thought > 0 || output > 0 else { return nil }
            return TokenDaySummary(
                dayStart: day,
                inputTokens: input,
                thoughtTokens: thought,
                outputTokens: output,
                costUSD: cost
            )
        }
    }

    private var tokenRows: [HistoryChartRow] {
        tokenDaySummaries.flatMap { day in
            [
                HistoryChartRow(
                    day: day.dayStart,
                    category: "Input",
                    value: Double(day.inputTokens)
                ),
                HistoryChartRow(
                    day: day.dayStart,
                    category: "Thought",
                    value: Double(day.thoughtTokens)
                ),
                HistoryChartRow(
                    day: day.dayStart,
                    category: "Output",
                    value: Double(day.outputTokens)
                )
            ]
        }.filter { $0.value > 0 }
    }

    private var totalPlaybackSeconds: Double {
        visibleDayStarts
            .compactMap { playbackDaysByStart[$0] }
            .reduce(0) { $0 + $1.totalPlaybackSeconds }
    }

    private var totalTokens: Int {
        tokenDaySummaries.reduce(0) { $0 + $1.totalTokens }
    }

    private var totalCost: Double {
        tokenDaySummaries.reduce(0) { $0 + $1.costUSD }
    }

    private var recentTokenRecords: [TokenUsageRecord] {
        Array(tokenRecords.suffix(10).reversed())
    }

    var body: some View {
        Form {
            if visibleDayStarts.isEmpty {
                ContentUnavailableView(
                    "No Usage Yet",
                    systemImage: "chart.bar.xaxis",
                    description: Text("Playback and token usage history will appear here after listening or running detection.")
                )
            } else {
                summarySection
                playbackSection
                tokenSection
                recentCallsSection
            }
        }
        .navigationTitle("Usage History")
        .navigationBarTitleDisplayMode(.inline)
    }

    private var summarySection: some View {
        Section("Last 30 Days") {
            LabeledContent("Playback") {
                Text(TimeFormatting.minutesDuration(totalPlaybackSeconds))
                    .foregroundStyle(.secondary)
                    .monospacedDigit()
            }
            LabeledContent("Tokens") {
                Text(formatTokens(totalTokens))
                    .foregroundStyle(.secondary)
                    .monospacedDigit()
            }
            LabeledContent("Estimated cost") {
                Text(formatCost(totalCost))
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

    private var tokenSection: some View {
        Section("Token Usage Per Day") {
            if tokenRows.isEmpty {
                Text("No token usage history yet.")
                    .foregroundStyle(.secondary)
            } else {
                Chart(tokenRows) { row in
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
        }
    }

    private var recentCallsSection: some View {
        Section("Recent Detection Calls") {
            if recentTokenRecords.isEmpty {
                Text("No token usage calls yet.")
                    .foregroundStyle(.secondary)
            } else {
                ForEach(recentTokenRecords) { record in
                    VStack(alignment: .leading, spacing: 4) {
                        HStack {
                            Text(record.episodeTitle ?? "Detection call")
                                .lineLimit(1)
                            Spacer()
                            Text(formatCost(record.totalCostUSD))
                                .foregroundStyle(.secondary)
                                .monospacedDigit()
                        }
                        Text("\(record.providerLabel) · \(formatTokens(record.totalTokens)) tokens")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                            .lineLimit(1)
                        Text(record.createdAt, format: .dateTime.month(.abbreviated).day().hour().minute())
                            .font(.caption2)
                            .foregroundStyle(.tertiary)
                    }
                }
            }
        }
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
}

private struct HistoryChartRow: Identifiable {
    let id = UUID()
    let day: Date
    let category: String
    let value: Double
}

private struct TokenDaySummary {
    let dayStart: Date
    let inputTokens: Int
    let thoughtTokens: Int
    let outputTokens: Int
    let costUSD: Double

    var totalTokens: Int {
        inputTokens + thoughtTokens + outputTokens
    }
}
