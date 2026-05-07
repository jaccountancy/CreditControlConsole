//
//  Credit_Control_ConsoleTests.swift
//  Credit Control ConsoleTests
//
//  Created by Jay Wilson on 07/05/2026.
//

import Foundation
import Testing
@testable import Credit_Control_Console

struct Credit_Control_ConsoleTests {
    @Test func dashboardPayloadDecodesSnakeCaseResponse() throws {
        let json = """
        {
          "as_of": "2026-05-07T18:30:00Z",
          "invoice_count": 4,
          "total_receivables": 12500.5,
          "total_overdue": 5000.0,
          "overdue_1_30": 2000.0,
          "overdue_31_60": 1500.0,
          "overdue_61_90": 750.0,
          "overdue_90_plus": 750.0,
          "accounts_needing_action": 3,
          "top_risk_accounts": [
            {
              "name": "Acme Limited",
              "amount_due": 2500.0,
              "due_date": "2026-05-01"
            }
          ]
        }
        """

        let payload = try JSONDecoder.dashboardDecoder.decode(DashboardPayload.self, from: Data(json.utf8))

        #expect(payload.invoiceCount == 4)
        #expect(payload.totalOverdue == 5000.0)
        #expect(payload.topRiskAccounts.first?.name == "Acme Limited")
    }
}
