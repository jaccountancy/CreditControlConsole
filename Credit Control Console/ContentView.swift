//
//  ContentView.swift
//  Credit Control Console
//
//  Created by Jay Wilson on 07/05/2026.
//

import SwiftUI

private enum SidebarDestination: String, CaseIterable, Identifiable {
    case dashboard = "Dashboard"
    case jentry = "Jentry"

    var id: String { rawValue }

    var iconName: String {
        switch self {
        case .dashboard:
            "rectangle.3.group"
        case .jentry:
            "square.grid.2x2"
        }
    }
}

struct ContentView: View {
    @Environment(\.openURL) private var openURL
    @State private var authenticationState: AuthenticationState = .signedOut
    @State private var dashboardState: DashboardLoadingState = .idle
    @State private var sessionToken = UserDefaults.standard.string(forKey: "BackendSessionToken") ?? ""
    @State private var signedInUser: DeviceUser?
    @State private var pollingTask: Task<Void, Never>?
    @State private var selectedDestination: SidebarDestination? = .dashboard

    var body: some View {
        Group {
            if sessionToken.isEmpty {
                NavigationStack {
                    loginView
                        .navigationTitle("Credit Control")
                }
            } else {
                NavigationSplitView {
                    List(SidebarDestination.allCases, selection: $selectedDestination) { destination in
                        Label(destination.rawValue, systemImage: destination.iconName)
                            .tag(destination)
                    }
                    .navigationTitle("Credit Control")
                } detail: {
                    detailView(for: selectedDestination ?? .dashboard)
                }
            }
        }
        .toolbar {
            if !sessionToken.isEmpty {
                ToolbarItemGroup {
                    if selectedDestination == .dashboard {
                        Button {
                            Task { await loadDashboard() }
                        } label: {
                            Label("Refresh", systemImage: "arrow.clockwise")
                        }
                    }

                    Button("Sign Out") {
                        signOut()
                    }
                }
            }
        }
        .task {
            if !sessionToken.isEmpty, case .idle = dashboardState {
                await loadDashboard()
            }
        }
    }

    @ViewBuilder
    private func detailView(for destination: SidebarDestination) -> some View {
        switch destination {
        case .dashboard:
            dashboardView
                .navigationTitle("Dashboard")
        case .jentry:
            JentryView()
                .navigationTitle("Jentry")
        }
    }

    @ViewBuilder
    private var loginView: some View {
        VStack(alignment: .leading, spacing: 18) {
            Text("Widget Dashboard Login")
                .font(.system(size: 34, weight: .bold, design: .rounded))

            Text("Authenticate against the credit control backend via Xero. The app only shows headline figures and risk widgets.")
                .foregroundStyle(.secondary)

            switch authenticationState {
            case .signedOut, .failed:
                if case let .failed(message) = authenticationState {
                    Text(message)
                        .foregroundStyle(.red)
                }

                Button("Login with Xero") {
                    Task { await startDeviceLogin() }
                }
                .buttonStyle(.borderedProminent)

            case let .authorising(device):
                VStack(alignment: .leading, spacing: 12) {
                    Text("Verification code: \(device.verificationCode)")
                        .font(.title2.monospaced())

                    Text("1. Open Xero in your browser.\n2. Sign in and approve access.\n3. Return to this app while it finishes login.")
                        .foregroundStyle(.secondary)

                    HStack {
                        Button("Open Xero Login") {
                            openURL(device.loginURI)
                        }
                        .buttonStyle(.borderedProminent)

                        Button("Open Approval Page") {
                            openURL(device.verificationURI)
                        }
                        .buttonStyle(.bordered)
                    }
                }

            case let .signedIn(user):
                Text("Signed in as \(user?.fullName ?? "authorised user").")
            }
        }
        .frame(maxWidth: 560, alignment: .leading)
        .padding(32)
    }

    @ViewBuilder
    private var dashboardView: some View {
        switch dashboardState {
        case .idle, .loading:
            ProgressView("Loading dashboard...")
                .frame(maxWidth: .infinity, maxHeight: .infinity)
        case let .failed(message):
            VStack(spacing: 16) {
                ContentUnavailableView(
                    "Unable to Load Dashboard",
                    systemImage: "exclamationmark.triangle",
                    description: Text(message)
                )
                Button("Try Again") {
                    Task { await loadDashboard() }
                }
            }
        case let .loaded(dashboard):
            ScrollView {
                VStack(alignment: .leading, spacing: 24) {
                    VStack(alignment: .leading, spacing: 8) {
                        Text("Headline Numbers")
                            .font(.system(size: 34, weight: .bold, design: .rounded))

                        if let signedInUser {
                            Text("Signed in as \(signedInUser.fullName)")
                                .foregroundStyle(.secondary)
                        }

                        if let asOf = dashboard.asOf {
                            Text("As of \(asOf.formatted(date: .abbreviated, time: .shortened))")
                                .foregroundStyle(.secondary)
                        }
                    }

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
                        metricCard(title: "1-30 Days", value: dashboard.overdue1To30, tint: .yellow)
                        metricCard(title: "31-60 Days", value: dashboard.overdue31To60, tint: .mint)
                        metricCard(title: "61-90 Days", value: dashboard.overdue61To90, tint: .teal)
                        metricCard(title: "90+ Days", value: dashboard.overdue90Plus, tint: .pink)
                    }

                    VStack(alignment: .leading, spacing: 12) {
                        Text("Top Risk Accounts")
                            .font(.title2.weight(.semibold))

                        ForEach(dashboard.topRiskAccounts) { account in
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
                .padding(24)
            }
        }
    }

    @ViewBuilder
    private func metricCard(title: String, value: Double, tint: Color, isCurrency: Bool = true) -> some View {
        VStack(alignment: .leading, spacing: 12) {
            Text(title)
                .font(.headline)
                .foregroundStyle(.secondary)

            Text(
                isCurrency
                    ? value.formatted(.currency(code: "GBP"))
                    : value.formatted(.number.precision(.fractionLength(0)))
            )
            .font(.system(size: 28, weight: .semibold, design: .rounded))
            .foregroundStyle(tint)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(20)
        .background(
            RoundedRectangle(cornerRadius: 20, style: .continuous)
                .fill(tint.opacity(0.08))
        )
    }

    @MainActor
    private func startDeviceLogin() async {
        do {
            let service = try configuredService()
            let device = try await service.startDeviceLogin()
            authenticationState = .authorising(device)
            beginPolling(deviceCode: device.deviceCode, service: service)
        } catch {
            authenticationState = .failed(error.localizedDescription)
        }
    }

    private func beginPolling(deviceCode: String, service: BackendService) {
        pollingTask?.cancel()
        pollingTask = Task {
            while !Task.isCancelled {
                do {
                    let response = try await service.pollDeviceLogin(deviceCode: deviceCode)
                    if response.status == "approved", let token = response.sessionToken {
                        await MainActor.run {
                            sessionToken = token
                            UserDefaults.standard.set(token, forKey: "BackendSessionToken")
                            signedInUser = response.user
                            authenticationState = .signedIn(response.user)
                            selectedDestination = .dashboard
                        }
                        await loadDashboard()
                        return
                    }
                } catch {
                    await MainActor.run {
                        authenticationState = .failed(error.localizedDescription)
                    }
                    return
                }

                try? await Task.sleep(for: .seconds(3))
            }
        }
    }

    @MainActor
    private func loadDashboard() async {
        dashboardState = .loading

        do {
            let dashboard = try await configuredService().loadDashboard(sessionToken: sessionToken)
            dashboardState = .loaded(dashboard)
        } catch {
            dashboardState = .failed(error.localizedDescription)
        }
    }

    private func signOut() {
        pollingTask?.cancel()
        sessionToken = ""
        signedInUser = nil
        dashboardState = .idle
        authenticationState = .signedOut
        selectedDestination = .dashboard
        UserDefaults.standard.removeObject(forKey: "BackendSessionToken")
    }

    private func configuredService() throws -> BackendService {
        guard
            let baseURLString = Bundle.main.object(forInfoDictionaryKey: "DashboardAPIBaseURL") as? String,
            let baseURL = URL(string: baseURLString)
        else {
            throw BackendServiceError.invalidConfiguration
        }

        return BackendService(baseURL: baseURL)
    }
}

private struct JentryView: View {
    var body: some View {
        Color.clear
            .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}

#Preview {
    ContentView()
}
