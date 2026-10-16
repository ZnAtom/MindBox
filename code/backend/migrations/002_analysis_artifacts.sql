ALTER TABLE processing_runs ADD COLUMN artifacts_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(artifacts_json));
