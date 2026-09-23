-- Clinical Co-Pilot review queue (W2 design spec §6).
-- Facts read off an uploaded document, waiting for a clinician. Columns match agent/copilot/schemas.py StagedFact.
-- Run once as a MySQL admin over a verified TLS session.
--
-- WHY THIS IS NOT THE AUDIT TABLE
-- copilot_audit.sql grants the agent INSERT and nothing else, because an audit log its writer can amend is not
-- evidence. This table needs SELECT to render the queue and UPDATE to record a decision. It also holds clinical
-- values, which the audit table deliberately never does, and it clears resolved rows rather than retaining them
-- for six years. Different rights, different retention, different data — so a separate table and a separate
-- grant. MySQL grants are per-table, so the agent's existing connection machinery is reused.

CREATE DATABASE IF NOT EXISTS copilot CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS copilot.copilot_staged_fact (
  id           BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  document_id  VARCHAR(64)  NOT NULL,          -- OpenEMR document id: the citation anchor
  field_path   VARCHAR(128) NOT NULL,          -- e.g. allergies[0].substance
  fact_kind    VARCHAR(32)  NOT NULL,          -- allergy | medication | medical_problem | lab
  payload      JSON         NOT NULL,          -- the write body, as the OpenEMR standard API expects it
  citation     JSON         NOT NULL,          -- source_type, page, field, quote, bbox (null when unlocated)
  confidence   DECIMAL(4,3) NOT NULL,
  status       VARCHAR(16)  NOT NULL DEFAULT 'pending',   -- pending | approved | rejected
  decided_by   VARCHAR(255) NULL,              -- fhirUser of the clinician who decided
  decided_at   DATETIME     NULL,
  created_at   DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  -- The idempotency key. Re-ingesting the same document cannot create a second pending row for the same fact,
  -- and cannot resurrect one a clinician already decided (PRD: "without creating duplicate or untraceable
  -- records"). The application relies on this constraint rather than on checking first, so a concurrent
  -- re-upload cannot slip between the check and the insert.
  UNIQUE KEY uq_document_field (document_id, field_path),
  KEY idx_status (status),
  KEY idx_document (document_id)
) ENGINE=InnoDB;

-- Agent queue user: SELECT, INSERT and UPDATE on this one table, TLS required. No DELETE — a rejected
-- extraction is kept, because it is a labelled example of the model being wrong and therefore an eval case.
-- Supply the password in the same session first, e.g. from `openssl rand -base64 32`, and store it only in the
-- agent's STAGING_DB_PASSWORD service variable (never commit it):
--   { echo "SET @copilot_staging_password = '<password>';"; cat deploy/sql/copilot_staged_fact.sql; } | mysql ...
-- Unset or empty, the statements below read `IDENTIFIED BY NULL` and fail, so the account is never created with
-- a known password. Rerunning with a new value rotates it (ALTER USER), where IF NOT EXISTS alone would skip it.
SET @copilot_staging_pw = QUOTE(NULLIF(@copilot_staging_password, ''));
SET @copilot_staging_sql = CONCAT('CREATE USER IF NOT EXISTS ''copilot_staging''@''%'' IDENTIFIED BY ',
                                  @copilot_staging_pw, ' REQUIRE SSL');
PREPARE copilot_staging_stmt FROM @copilot_staging_sql;
EXECUTE copilot_staging_stmt;
DEALLOCATE PREPARE copilot_staging_stmt;

SET @copilot_staging_alter = CONCAT('ALTER USER ''copilot_staging''@''%'' IDENTIFIED BY ', @copilot_staging_pw,
                                    ' REQUIRE SSL');
PREPARE copilot_staging_alter_stmt FROM @copilot_staging_alter;
EXECUTE copilot_staging_alter_stmt;
DEALLOCATE PREPARE copilot_staging_alter_stmt;

GRANT SELECT, INSERT, UPDATE ON copilot.copilot_staged_fact TO 'copilot_staging'@'%';
FLUSH PRIVILEGES;
