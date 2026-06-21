//
//  ContentView.swift
//  Credit Control Console
//
//  Created by Jay Wilson on 07/05/2026.
//

import SwiftUI

private enum BrandPalette {
    static let navy = Color(red: 0.06, green: 0.14, blue: 0.24)
    static let blue = Color(red: 0.204, green: 0.518, blue: 0.773)
    static let cyan = Color(red: 0.0, green: 0.68, blue: 0.83)
    static let mist = Color(red: 0.93, green: 0.96, blue: 0.99)
    static let card = Color.white.opacity(0.92)
}

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
    @State private var loginAssistiveMessage: String?
    @State private var loginAssistiveIsError = false

    var body: some View {
        Group {
            if sessionToken.isEmpty {
                NavigationStack {
                    loginView
                        .navigationTitle("Jenius Tools")
                        .navigationBarTitleDisplayMode(.inline)
                }
            } else {
                NavigationSplitView {
                    ZStack {
                        LinearGradient(
                            colors: [BrandPalette.navy, BrandPalette.blue],
                            startPoint: .topLeading,
                            endPoint: .bottomTrailing
                        )
                        .ignoresSafeArea()

                        VStack(alignment: .leading, spacing: 16) {
                            HStack {
                                brandHorizontalLogo(height: 30)
                                Spacer(minLength: 0)
                            }
                            .padding(.horizontal, 14)
                            .padding(.top, 12)

                            VStack(alignment: .leading, spacing: 8) {
                                ForEach(SidebarDestination.allCases) { destination in
                                    Button {
                                        selectedDestination = destination
                                    } label: {
                                        Label(destination.rawValue, systemImage: destination.iconName)
                                            .font(.headline)
                                            .frame(maxWidth: .infinity, alignment: .leading)
                                            .padding(.horizontal, 12)
                                            .padding(.vertical, 10)
                                            .foregroundStyle(.white)
                                    }
                                    .buttonStyle(.plain)
                                    .background(
                                        RoundedRectangle(cornerRadius: 12, style: .continuous)
                                            .fill(selectedDestination == destination ? Color.white.opacity(0.24) : Color.white.opacity(0.12))
                                    )
                                }
                                Spacer(minLength: 0)
                            }
                            .padding(.horizontal, 10)
                        }
                    }
                    .navigationTitle("Jenius Tools")
                } detail: {
                    detailView(for: selectedDestination ?? .dashboard)
                        .background(BrandPalette.mist)
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
        ZStack {
            LinearGradient(
                colors: [BrandPalette.navy, BrandPalette.blue, BrandPalette.cyan],
                startPoint: .topLeading,
                endPoint: .bottomTrailing
            )
            .ignoresSafeArea()

            Circle()
                .fill(Color.white.opacity(0.10))
                .frame(width: 360, height: 360)
                .offset(x: -220, y: -260)

            Circle()
                .fill(Color.white.opacity(0.10))
                .frame(width: 320, height: 320)
                .offset(x: 220, y: 220)

            VStack(alignment: .leading, spacing: 20) {
                VStack(alignment: .leading, spacing: 8) {
                    brandHorizontalLogo(height: 32)

                    Text("Jenius Tools")
                        .font(.system(size: 36, weight: .bold, design: .rounded))
                        .foregroundStyle(BrandPalette.navy)

                    Text("Sign in with your linked Xero identity, then approve access in the Jenius Auth iPhone app.")
                        .foregroundStyle(BrandPalette.navy.opacity(0.8))
                }

                switch authenticationState {
                case .signedOut, .failed:
                    VStack(alignment: .leading, spacing: 12) {
                        if case let .failed(message) = authenticationState {
                            Text(message)
                                .foregroundStyle(Color.red.opacity(0.92))
                                .font(.subheadline.weight(.semibold))
                        }

                        HStack(spacing: 10) {
                            Text("Step 1")
                                .font(.caption.weight(.bold))
                                .padding(.horizontal, 10)
                                .padding(.vertical, 6)
                                .foregroundStyle(BrandPalette.blue)
                                .background(BrandPalette.blue.opacity(0.12), in: Capsule())
                            Text("Continue with Xero")
                                .foregroundStyle(BrandPalette.navy.opacity(0.92))
                        }

                        Button("Continue with Xero") {
                            Task { await startDeviceLogin() }
                        }
                        .buttonStyle(.borderedProminent)
                        .tint(BrandPalette.cyan)
                    }

                case let .authorising(device):
                    VStack(alignment: .leading, spacing: 14) {
                        HStack(spacing: 10) {
                            Text("Step 2")
                                .font(.caption.weight(.bold))
                                .padding(.horizontal, 10)
                                .padding(.vertical, 6)
                                .foregroundStyle(BrandPalette.blue)
                                .background(BrandPalette.blue.opacity(0.12), in: Capsule())
                            Text("Approve in Browser + App")
                                .foregroundStyle(BrandPalette.navy.opacity(0.92))
                        }

                        Text("Open Xero login in your browser, then approve sign-in from your Jenius Auth iPhone app to complete access.")
                            .foregroundStyle(BrandPalette.navy.opacity(0.82))

                        HStack {
                            Button("Open Xero Login") {
                                openExternalURL(device.loginURI, actionLabel: "Xero login")
                            }
                            .buttonStyle(.borderedProminent)
                            .tint(BrandPalette.cyan)

                            Button("Open Approval Page") {
                                openExternalURL(device.verificationURI, actionLabel: "approval page")
                            }
                            .buttonStyle(.bordered)
                            .tint(BrandPalette.blue)
                        }

                        if let loginAssistiveMessage {
                            Label(loginAssistiveMessage, systemImage: loginAssistiveIsError ? "exclamationmark.triangle.fill" : "checkmark.circle.fill")
                                .font(.subheadline.weight(.semibold))
                                .foregroundStyle(loginAssistiveIsError ? Color.red.opacity(0.9) : BrandPalette.blue.opacity(0.92))
                        }
                    }

                case let .signedIn(user):
                    Label("Signed in as \(user?.fullName ?? "authorised user").", systemImage: "checkmark.seal.fill")
                        .foregroundStyle(BrandPalette.blue)
                        .font(.headline)
                }
            }
            .frame(maxWidth: 620, alignment: .leading)
            .padding(32)
            .background(BrandPalette.card, in: RoundedRectangle(cornerRadius: 28, style: .continuous))
            .shadow(color: .black.opacity(0.25), radius: 24, y: 16)
        }
        .padding(40)
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
                    VStack(alignment: .leading, spacing: 14) {
                        brandHorizontalLogo(height: 28)

                        Text("Jenius Tools Overview")
                            .font(.system(size: 34, weight: .bold, design: .rounded))
                            .foregroundStyle(BrandPalette.navy)

                        HStack(spacing: 10) {
                            if let signedInUser {
                                Label(signedInUser.fullName, systemImage: "person.crop.circle.fill")
                                    .font(.subheadline.weight(.semibold))
                                    .foregroundStyle(BrandPalette.navy.opacity(0.85))
                            }

                            if let asOf = dashboard.asOf {
                                Label(
                                    "Updated \(asOf.formatted(date: .abbreviated, time: .shortened))",
                                    systemImage: "clock.arrow.circlepath"
                                )
                                .font(.subheadline)
                                .foregroundStyle(.secondary)
                            }
                        }
                    }
                    .padding(20)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .background(
                        RoundedRectangle(cornerRadius: 22, style: .continuous)
                            .fill(
                                LinearGradient(
                                    colors: [Color.white, BrandPalette.cyan.opacity(0.12)],
                                    startPoint: .topLeading,
                                    endPoint: .bottomTrailing
                                )
                            )
                    )
                    .overlay(
                        RoundedRectangle(cornerRadius: 22, style: .continuous)
                            .stroke(BrandPalette.blue.opacity(0.15), lineWidth: 1)
                    )

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
                            .foregroundStyle(BrandPalette.navy)

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
                                    .foregroundStyle(BrandPalette.navy)
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
            .foregroundStyle(tint.gradient)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(20)
        .background(
            RoundedRectangle(cornerRadius: 20, style: .continuous)
                .fill(
                    LinearGradient(
                        colors: [Color.white, tint.opacity(0.16)],
                        startPoint: .topLeading,
                        endPoint: .bottomTrailing
                    )
                )
        )
        .overlay(
            RoundedRectangle(cornerRadius: 20, style: .continuous)
                .stroke(tint.opacity(0.24), lineWidth: 1)
        )
    }

    @ViewBuilder
    private func brandHorizontalLogo(height: CGFloat) -> some View {
        Image("JaccountancyBlueHorizontal_1")
            .resizable()
            .scaledToFit()
            .frame(height: height)
            .accessibilityLabel("Jaccountancy")
    }

    @MainActor
    private func startDeviceLogin() async {
        do {
            loginAssistiveMessage = nil
            loginAssistiveIsError = false
            let service = try configuredService()
            let device = try await service.startDeviceLogin()
            authenticationState = .authorising(device)
            openExternalURL(device.loginURI, actionLabel: "Xero login")
            beginPolling(deviceCode: device.deviceCode, service: service)
        } catch {
            authenticationState = .failed(error.localizedDescription)
        }
    }

    private func openExternalURL(_ url: URL, actionLabel: String) {
        openURL(url) { accepted in
            if accepted {
                loginAssistiveIsError = false
                loginAssistiveMessage = "Opened \(actionLabel). Complete that step, then return to Jenius."
            } else {
                loginAssistiveIsError = true
                loginAssistiveMessage = "Could not open \(actionLabel). Check browser restrictions and try again."
            }
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
        loginAssistiveMessage = nil
        loginAssistiveIsError = false
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
