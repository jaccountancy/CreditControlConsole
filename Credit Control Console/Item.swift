//
//  Item.swift
//  Credit Control Console
//
//  Created by Jay Wilson on 07/05/2026.
//

import Foundation

struct DashboardPayload: Decodable {
    let asOf: Date?
    let invoiceCount: Int
    let totalReceivables: Double
    let totalOverdue: Double
    let overdue1To30: Double
    let overdue31To60: Double
    let overdue61To90: Double
    let overdue90Plus: Double
    let accountsNeedingAction: Int
    let topRiskAccounts: [TopRiskAccount]

    enum CodingKeys: String, CodingKey {
        case asOf = "as_of"
        case invoiceCount = "invoice_count"
        case totalReceivables = "total_receivables"
        case totalOverdue = "total_overdue"
        case overdue1To30 = "overdue_1_30"
        case overdue31To60 = "overdue_31_60"
        case overdue61To90 = "overdue_61_90"
        case overdue90Plus = "overdue_90_plus"
        case accountsNeedingAction = "accounts_needing_action"
        case topRiskAccounts = "top_risk_accounts"
    }
}

struct TopRiskAccount: Decodable, Identifiable {
    let name: String
    let amountDue: Double
    let dueDate: Date?

    var id: String { "\(name)-\(dueDate?.timeIntervalSince1970 ?? 0)" }

    enum CodingKeys: String, CodingKey {
        case name
        case amountDue = "amount_due"
        case dueDate = "due_date"
    }
}

struct DeviceLoginPayload: Decodable {
    let deviceCode: String
    let verificationCode: String
    let verificationURI: URL
    let loginURI: URL

    enum CodingKeys: String, CodingKey {
        case deviceCode = "device_code"
        case verificationCode = "verification_code"
        case verificationURI = "verification_uri"
        case loginURI = "login_uri"
    }
}

struct DevicePollResponse: Decodable {
    let status: String
    let sessionToken: String?
    let user: DeviceUser?

    enum CodingKeys: String, CodingKey {
        case status
        case sessionToken = "session_token"
        case user
    }
}

struct DeviceUser: Decodable {
    let email: String
    let fullName: String

    enum CodingKeys: String, CodingKey {
        case email
        case fullName = "full_name"
    }
}

enum DashboardLoadingState {
    case idle
    case loading
    case loaded(DashboardPayload)
    case failed(String)
}

enum AuthenticationState {
    case signedOut
    case authorising(DeviceLoginPayload)
    case signedIn(DeviceUser?)
    case failed(String)
}

struct BackendService {
    private let baseURL: URL
    private let session: URLSession

    init(baseURL: URL, session: URLSession = .shared) {
        self.baseURL = baseURL
        self.session = session
    }

    func startDeviceLogin() async throws -> DeviceLoginPayload {
        let (data, response) = try await session.data(from: baseURL.appending(path: "/api/device/start"))
        try validate(response: response)
        return try JSONDecoder.dashboardDecoder.decode(DeviceLoginPayload.self, from: data)
    }

    func pollDeviceLogin(deviceCode: String) async throws -> DevicePollResponse {
        let endpoint = baseURL.appending(path: "/api/device/poll").appending(queryItems: [
            URLQueryItem(name: "device_code", value: deviceCode),
        ])
        let (data, response) = try await session.data(from: endpoint)
        try validate(response: response)
        return try JSONDecoder.dashboardDecoder.decode(DevicePollResponse.self, from: data)
    }

    func loadDashboard(sessionToken: String) async throws -> DashboardPayload {
        var request = URLRequest(url: baseURL.appending(path: "/api/dashboard"))
        request.setValue("Bearer \(sessionToken)", forHTTPHeaderField: "Authorization")
        let (data, response) = try await session.data(for: request)
        try validate(response: response)
        return try JSONDecoder.dashboardDecoder.decode(DashboardPayload.self, from: data)
    }

    private func validate(response: URLResponse) throws {
        guard let http = response as? HTTPURLResponse else {
            throw URLError(.badServerResponse)
        }

        guard (200 ..< 300).contains(http.statusCode) else {
            throw BackendServiceError.invalidStatus(http.statusCode)
        }
    }
}

enum BackendServiceError: LocalizedError {
    case invalidConfiguration
    case invalidStatus(Int)

    var errorDescription: String? {
        switch self {
        case .invalidConfiguration:
            "Missing backend configuration."
        case let .invalidStatus(code):
            "Backend request failed with status \(code)."
        }
    }
}

extension JSONDecoder {
    static var dashboardDecoder: JSONDecoder {
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .custom { decoder in
            let container = try decoder.singleValueContainer()
            let value = try container.decode(String.self)

            let iso8601Formatter = ISO8601DateFormatter()
            if let date = iso8601Formatter.date(from: value) {
                return date
            }

            let dateFormatter = DateFormatter()
            dateFormatter.calendar = Calendar(identifier: .iso8601)
            dateFormatter.locale = Locale(identifier: "en_US_POSIX")
            dateFormatter.timeZone = TimeZone(secondsFromGMT: 0)
            dateFormatter.dateFormat = "yyyy-MM-dd"

            if let date = dateFormatter.date(from: value) {
                return date
            }

            throw DecodingError.dataCorruptedError(
                in: container,
                debugDescription: "Unsupported date format: \(value)"
            )
        }
        return decoder
    }
}
