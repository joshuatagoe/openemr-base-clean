-- Clinical Co-Pilot module schema, version 0.2.0 (ADR-009 section 6, ADR-012).
-- Applied by bin/copilot-migrate.php through OpenEMR's SQLUpgradeService, so the
-- #IfNotTable / #IfNotRow2D guards make a re-run a no-op. Comments stay on their
-- own lines: SQLUpgradeService joins a statement's lines with spaces, so an inline
-- "--" comment would swallow the rest of the statement.
-- No destructive statements here or in any automatic upgrade file.

-- ADR-012 processing record, one per stored OpenEMR document.
-- doc_type: lab_pdf | intake_form | unsupported
-- status: queued | processing | extracted | failed | skipped_duplicate | unsupported | held_identity
-- last_error_code: fixed codes only, never messages
-- identity_check: match | mismatch | missing
-- extraction_json: the validated extraction (results, citations, boxes), reused by briefings
#IfNotTable copilot_document
CREATE TABLE `copilot_document` (
  `document_id` BIGINT NOT NULL PRIMARY KEY,
  `pid` BIGINT NOT NULL,
  `content_sha256` CHAR(64) NOT NULL,
  `doc_type` VARCHAR(20) NOT NULL,
  `status` VARCHAR(20) NOT NULL,
  `prompt_version` VARCHAR(40) NULL,
  `attempts` SMALLINT NOT NULL DEFAULT 0,
  `last_error_code` VARCHAR(64) NULL,
  `identity_check` VARCHAR(12) NULL,
  `extraction_json` MEDIUMTEXT NULL,
  `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` DATETIME NULL,
  KEY `idx_pid_sha` (`pid`, `content_sha256`),
  KEY `idx_sha` (`content_sha256`),
  KEY `idx_status` (`status`)
) ENGINE=InnoDB;
#EndIf

-- ADR-009 candidates, one per extracted value.
-- value_text is NULL when unreadable; flag_source: extracted | derived | unavailable
-- bbox: "x0,y0,x1,y1", 0-1, top-left origin, cropbox-relative
-- status: candidate | filed | rejected
#IfNotTable copilot_extracted_value
CREATE TABLE `copilot_extracted_value` (
  `id` BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
  `document_id` BIGINT NOT NULL,
  `pid` BIGINT NOT NULL,
  `result_index` SMALLINT NOT NULL,
  `test_name` VARCHAR(255) NOT NULL,
  `value_text` VARCHAR(255) NULL,
  `unit` VARCHAR(50) NULL,
  `reference_range` VARCHAR(100) NULL,
  `abnormal_flag` VARCHAR(4) NULL,
  `flag_source` VARCHAR(12) NULL,
  `collection_date` DATE NULL,
  `verification_status` VARCHAR(20) NOT NULL,
  `page` SMALLINT NULL,
  `bbox` VARCHAR(80) NULL,
  `status` VARCHAR(12) NOT NULL DEFAULT 'candidate',
  `filed_value` VARCHAR(255) NULL,
  `filed_by` BIGINT NULL,
  `filed_at` DATETIME NULL,
  `procedure_result_id` BIGINT NULL,
  UNIQUE KEY `uq_doc_index` (`document_id`, `result_index`),
  KEY `idx_pid_status` (`pid`, `status`)
) ENGINE=InnoDB;
#EndIf

-- ADR-009 section 7: un-filing writes result_status 'entered-in-error'; list it so the native dropdown shows it.
#IfNotRow2D list_options list_id proc_res_status option_id entered-in-error
INSERT INTO list_options (list_id, option_id, title, seq, activity) VALUES ('proc_res_status', 'entered-in-error', 'Entered in error', 45, 1);
#EndIf
