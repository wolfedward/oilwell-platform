# Task 4: Local Authentication, Roles, and Audit Trail

## RED / GREEN evidence

- RED: `python -m unittest tests.test_auth -v` initially failed with `ModuleNotFoundError: No module named 'backend.audit'`, before either production module existed.
- GREEN: `python -m unittest tests.test_auth -v` completed `Ran 10 tests ... OK` after implementation.
- Regression: database, seed, and health suites passed during focused checks; final `python -m unittest discover -v` completed `Ran 53 tests in 90.701s` with `OK`.
- Syntax: `python -m py_compile backend/auth.py backend/audit.py backend/database.py backend/seed.py backend/http_server.py server.py` completed successfully.

## Authentication and roles

- Passwords use PBKDF2-HMAC-SHA256 with a per-user random 16-byte salt (hex encoded) and 300,000 iterations. The database never stores a plaintext password.
- A login generates a random 32-byte token, returns the token to the caller, and stores only its SHA-256 hash. Sessions expire after eight hours and reject expired, revoked, disabled-user, and disabled-role access.
- `administrator` has explicit user/role administration permissions. `reviewer` only has read permissions and cannot approve strategies. The other least-privilege role sets are production, algorithm, and maintenance engineering.

Initial local accounts:

| Role | Username | Initial demonstration password |
| --- | --- | --- |
| Administrator | `administrator` | `Admin@2026` |
| Production engineer | `production_engineer` | `Production@2026` |
| Algorithm engineer | `algorithm_engineer` | `Algorithm@2026` |
| Maintenance engineer | `maintenance_engineer` | `Maintenance@2026` |
| Reviewer | `reviewer` | `Review@2026` |

No hash, salt, or token is included in this report or any public API response.

## API contract

- `POST /api/auth/login` accepts a JSON object with `username` and `password`, returns `{ok,data}` and sets `platform_session` with `HttpOnly`, `SameSite=Strict`, `Path=/`, and `Max-Age=28800`.
- `POST /api/auth/logout` revokes the session and expires the cookie; `GET /api/auth/me` returns a public user shape. Protected routes accept the cookie or `Authorization: Bearer <token>`.
- `GET/POST/PATCH /api/users` and `/api/users/{id}`, plus matching role routes, return the common envelope. Missing authentication is 401, missing permission 403, stale version 409, malformed JSON/body 400, and field validation 422.
- `PATCH` requires a positive current `version` and updates using `WHERE id=? AND version=?`, incrementing `version` atomically. It supports enable/disable, role changes, display-name changes, password reset, and role permission changes. The last usable administrator cannot be disabled and the administrator role cannot lose its user/role management permissions.

## Audit atomicity and privacy

`AuditService.record` canonicalizes JSON (`sort_keys`, compact separators), removes secret-bearing fields, and always retains `before` and `after` objects. Mutating HTTP handlers use one `Database.transaction(..., auto_audit=False)` connection for the business update and explicit audit insert; an exception rolls back both. Authentication, logout, and seed-enrichment writes use the same audited transaction boundary. Read-only requests do not emit audit records.

## Migration and seed strategy

The migration adds password/version/audit-detail fields with additive `ALTER TABLE` checks and creates `permissions`, `role_permissions`, and `sessions`; it never rebuilds users, roles, or business tables. `seed_database` validates the existing v2 Task 3 fingerprint before idempotently enriching authentication fields and role permissions. Password material and role grants are intentionally outside the Task 3 manifest projection, so the already-generated v2 manifest remains valid and business records are not overwritten. The enrichment itself leaves an explicit `auth.bootstrap` audit event only when it actually changes data.

## Residual risk

This is an offline local service: demonstration passwords must be changed before use outside the controlled acceptance environment. SQLite serializes writes and has a busy timeout, but it is not intended as a multi-host identity service. Transport is loopback/local deployment rather than a TLS termination layer.
