-- Clinical Co-Pilot audit table (ARCHITECTURE.md §7, AUDIT.md COMP-7).
-- Columns match agent/schemas.py AuditEvent. No clinical values, no question or answer text.
-- Run once as a MySQL admin over a verified TLS session. Retention target: 6 years.
-- Tamper evidence is limited to database permissions: the agent's user can only INSERT.

CREATE DATABASE IF NOT EXISTS copilot CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS copilot.copilot_audit (
  id             BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  event          VARCHAR(32)  NOT NULL,  -- launch | session_create | question | fhir_read | llm_call | refusal | denied
  ts_ms          BIGINT       NOT NULL,  -- UTC epoch milliseconds
  correlation_id VARCHAR(64)  NOT NULL,
  session_ref    VARCHAR(64)  NULL,      -- random reference, never the session handle
  fhir_user      VARCHAR(255) NULL,      -- Practitioner/... or Person/...
  client_id      VARCHAR(255) NULL,
  source         VARCHAR(16)  NULL,      -- launch | api | schedule
  patient_id     VARCHAR(64)  NULL,      -- patient uuid
  intent         VARCHAR(32)  NULL,      -- enum, never question text
  fhir_path      VARCHAR(255) NULL,      -- path without query string
  http_status    SMALLINT     NULL,
  record_count   INT          NULL,
  outcome        VARCHAR(32)  NULL,
  detail         VARCHAR(200) NULL,      -- non-PHI reason code
  PRIMARY KEY (id),
  KEY idx_ts (ts_ms),
  KEY idx_user_ts (fhir_user, ts_ms),
  KEY idx_patient_ts (patient_id, ts_ms),
  KEY idx_correlation (correlation_id)
) ENGINE=InnoDB;

-- Agent writer: INSERT only on this one table, TLS required. No SELECT, UPDATE or DELETE.
-- Supply the password in the same session first, e.g. from `openssl rand -base64 32`, and store it only in the agent's
-- AUDIT_DB_PASSWORD service variable (never commit it):
--   { echo "SET @copilot_audit_password = '<password>';"; cat deploy/sql/copilot_audit.sql; } | mysql ...
-- Unset or empty, the statements below read `IDENTIFIED BY NULL` and fail, so the account is never created with a
-- known password. Rerunning with a new value rotates it (ALTER USER), where IF NOT EXISTS alone would skip it.
SET @copilot_audit_pw = QUOTE(NULLIF(@copilot_audit_password, ''));
SET @copilot_audit_sql = CONCAT('CREATE USER IF NOT EXISTS ''copilot_audit''@''%'' IDENTIFIED BY ', @copilot_audit_pw,
                                ' REQUIRE SSL');
PREPARE copilot_audit_stmt FROM @copilot_audit_sql;
EXECUTE copilot_audit_stmt;
SET @copilot_audit_sql = CONCAT('ALTER USER ''copilot_audit''@''%'' IDENTIFIED BY ', @copilot_audit_pw, ' REQUIRE SSL');
PREPARE copilot_audit_stmt FROM @copilot_audit_sql;
EXECUTE copilot_audit_stmt;
DEALLOCATE PREPARE copilot_audit_stmt;
SET @copilot_audit_password = NULL, @copilot_audit_pw = NULL, @copilot_audit_sql = NULL;
GRANT INSERT ON copilot.copilot_audit TO 'copilot_audit'@'%';

-- Read-only reviewer (compliance / incident response), never used by the agent. Uncomment, set a password and run
-- separately:
-- CREATE ROLE IF NOT EXISTS 'copilot_audit_reviewer';
-- GRANT SELECT ON copilot.copilot_audit TO 'copilot_audit_reviewer';
-- CREATE USER '<reviewer>'@'%' IDENTIFIED BY <quoted password> REQUIRE SSL;
-- GRANT 'copilot_audit_reviewer' TO '<reviewer>'@'%';
-- SET DEFAULT ROLE 'copilot_audit_reviewer' TO '<reviewer>'@'%';
