//
//  ContentView.swift
//  Credit Control Console
//
//  Created by Jay Wilson on 07/05/2026.
//

import SwiftUI

struct ContentView: View {
    @State private var loadingState: DashboardLoadingState = .idle

    var body: some View {
        NavigationStack {
            dashboardBody
                .toolbar {
                    ToolbarItem {
                        Button {
                            Task {
                                await loadDashboard()
                            }
                        } label: {
                            Label("Refresh", systemImage: "arrow.clockwise")
                        }
                    }
                }
                .navigationTitle("Credit Control")
                .task {
                    await loadDashboard()
                }
        }
    }

    @ViewBuilder
    private var dashboardBody: some View {
        switch loadingState {
        case .idle, .loading:
            ProgressView("Loading dashboard...")
                .frame(maxWidth: .infinity, maxHeight: .infinity)
        case let .failed(message):
            ContentUnavailableView(
                "Unable to Load Dashboard",
                systemImage: "exclamationmark.triangle",
                description: Text(message)
            )
        case let .loaded(dashboard):
            ScrollView {
                VStack(alignment: .leading, spacing: 24) {
                    headerView(asOf: dashboard.asOf)
                    metricsGrid(dashboard: dashboard)
                    riskSection(accounts: dashboard.topRiskAccounts)
                }
                .padding(24)
            }
        }
    }

    @ViewBuilder
    private func headerView(asOf: Date?) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("Headline Numbers")
                .font(.system(size: 34, weight: .bold, design: .rounded))

            if let asOf {
                Text("As of \(asOf.formatted(date: .abbreviated, time: .shortened))")
                    .foregroundStyle(.secondary)
            } else {
                Text("No sync has been completed yet.")
                    .foregroundStyle(.secondary)
            }
        }
    }

    @ViewBuilder
    private func metricsGrid(dashboard: DashboardPayload) -> some View {
        LazyVGrid(
            columns: [
                GridItem(.flexible(), spacing: 16),
                GridItem(.flexible(), spacing: 16),
            ],
            spacing: 16
        ) {
            metricCard(title: "Total Receivables", value: dashboard.totalReceivables, tint: .blue)
            metricCard(title: "Total Overdue", value: dashboard.totalOverdue, tint: .red)
            metricCard(title: "Invoices Open", value: Double(dashboard.invoiceCount), tint: .indigo, isCurrency: false)
            metricCard(title: "Accounts Needing Action", value: Double(dashboard.accountsNeedingAction), tint: .orange, isCurrency: false)
            metricCard(title: "1-30 Days Overdue", value: dashboard.overdue1To30, tint: .yellow)
            metricCard(title: "31-60 Days Overdue", value: dashboard.overdue31To60, tint: .mint)
            metricCard(title: "61-90 Days Overdue", value: dashboard.overdue61To90, tint: .teal)
            metricCard(title: "90+ Days Overdue", value: dashboard.overdue90Plus, tint: .pink)
        }
    }

    @ViewBuilder
    private func metricCard(title: String, value: Double, tint: Color, isCurrency: Bool = true) -> some View {
        VStack(alignment: .leading, spacing: 12) {
            Text(title)
                .font(.headline)
                .foregroundStyle(.secondary)

            Text(metricValueText(value: value, isCurrency: isCurrency))
                .font(.system(size: 28, weight: .semibold, design: .rounded))
                .foregroundStyle(tint)
                .lineLimit(1)
                .minimumScaleFactor(0.8)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(20)
        .background(
            RoundedRectangle(cornerRadius: 20, style: .continuous)
                .fill(tint.opacity(0.08))
        )
    }

    @ViewBuilder
    private func riskSection(accounts: [TopRiskAccount]) -> some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Top Risk Accounts")
                .font(.title2.weight(.semibold))

            if accounts.isEmpty {
                ContentUnavailableView(
                    "No Accounts to Show",
                    systemImage: "checkmark.circle",
                    description: Text("Run the Xero sync on the backend to populate live data.")
                )
            } else {
                ForEach(accounts) { account in
                    HStack {
                        VStack(alignment: .leading, spacing: 4) {
                            Text(account.name)
                                .font(.headline)

                            if let dueDate = account.dueDate {
                                Text("Due \(dueDate.formatted(date: .abbreviated, time: .omitted))")
                                    .foregroundStyle(.secondary)
                            }
                        }

                        Spacer()

                        Text(account.amountDue, format: .currency(code: "GBP"))
                            .font(.headline.monospacedDigit())
                    }
                    .padding(.vertical, 8)
                    Divider()
                }
            }
        }
    }

    private func metricValueText(value: Double, isCurrency: Bool) -> String {
        if isCurrency {
            value.formatted(.currency(code: "GBP"))
        } else {
            value.formatted(.number.precision(.fractionLength(0)))
        }
    }

    @MainActor
    private func loadDashboard() async {
        loadingState = .loading

        do {
            let dashboard = try await configuredService().loadDashboard()
            loadingState = .loaded(dashboard)
        } catch {
            loadingState = .failed(error.localizedDescription)
        }
    }

    private func configuredService() throws -> DashboardService {
        guard
            let baseURLString = Bundle.main.object(forInfoDictionaryKey: "DashboardAPIBaseURL") as? String,
            let baseURL = URL(string: baseURLString),
            let apiToken = Bundle.main.object(forInfoDictionaryKey: "DashboardAPIToken") as? String,
            !apiToken.isEmpty
        else {
            throw DashboardServiceError.invalidConfiguration
        }

        return DashboardService(baseURL: baseURL, apiToken: apiToken)
    }
}

#Preview {
    ContentView()
}
