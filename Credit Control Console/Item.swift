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

    var id: String { name }

    enum CodingKeys: String, CodingKey {
        case name
        case amountDue = "amount_due"
        case dueDate = "due_date"
    }
}

enum DashboardLoadingState {
    case idle
    case loading
    case loaded(DashboardPayload)
    case failed(String)
}

struct DashboardService {
    private let baseURL: URL
    private let apiToken: String
    private let session: URLSession

    init(
        baseURL: URL,
        apiToken: String,
        session: URLSession = .shared
    ) {
        self.baseURL = baseURL
        self.apiToken = apiToken
        self.session = session
    }

    func loadDashboard() async throws -> DashboardPayload {
        var request = URLRequest(url: baseURL.appending(path: "/api/dashboard"))
        request.setValue("Bearer \(apiToken)", forHTTPHeaderField: "Authorization")

        let (data, response) = try await session.data(for: request)
        guard let response = response as? HTTPURLResponse else {
            throw URLError(.badServerResponse)
        }

        guard (200 ..< 300).contains(response.statusCode) else {
            throw DashboardServiceError.invalidStatus(response.statusCode)
        }

        return try JSONDecoder.dashboardDecoder.decode(DashboardPayload.self, from: data)
    }
}

enum DashboardServiceError: LocalizedError {
    case invalidConfiguration
    case invalidStatus(Int)

    var errorDescription: String? {
        switch self {
        case .invalidConfiguration:
            "Missing dashboard API configuration."
        case let .invalidStatus(code):
            "Dashboard request failed with status \(code)."
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
