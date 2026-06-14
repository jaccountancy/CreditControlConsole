# Investigations

## Open

- [ ] Client Register crash on client page open
  - Symptom: UI shows `API request failed` when opening a client from Client Register.
  - Endpoint: `GET /api/companies-house/auth-code-register/{row_id}/client-page`
  - Current error: `500 Internal Server Error` (seen on June 14, 2026).
  - Notes: Investigate backend handler and payload assumptions for rows with missing or unexpected fields.
