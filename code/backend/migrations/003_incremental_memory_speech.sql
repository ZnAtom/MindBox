CREATE TABLE experiment_context (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    scope_id TEXT NOT NULL UNIQUE,
    mode TEXT NOT NULL CHECK (mode IN ('cold', 'warm', 'holdout', 'disabled')),
    manifest_json TEXT NOT NULL CHECK (json_valid(manifest_json))
);
CREATE TABLE product_profiles (
    id TEXT PRIMARY KEY,
    cabinet_id TEXT NOT NULL REFERENCES cabinets(id),
    status TEXT NOT NULL CHECK (status IN ('candidate', 'verified', 'rejected')),
    version INTEGER NOT NULL CHECK (version > 0),
    fields_json TEXT NOT NULL CHECK (json_valid(fields_json)),
    aliases_json TEXT NOT NULL CHECK (json_valid(aliases_json)),
    barcode TEXT,
    verification_json TEXT NOT NULL CHECK (json_valid(verification_json)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE product_examples (
    id TEXT PRIMARY KEY,
    product_id TEXT NOT NULL REFERENCES product_profiles(id),
    asset_path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    capture_hash TEXT NOT NULL,
    metadata_json TEXT NOT NULL CHECK (json_valid(metadata_json)),
    UNIQUE (product_id, sha256)
);
CREATE TABLE product_claims (
    id TEXT PRIMARY KEY,
    product_id TEXT NOT NULL REFERENCES product_profiles(id),
    field_name TEXT NOT NULL,
    value_json TEXT NOT NULL CHECK (json_valid(value_json)),
    provenance_json TEXT NOT NULL CHECK (json_valid(provenance_json))
);
CREATE TABLE item_product_links (
    item_id TEXT PRIMARY KEY REFERENCES items(id),
    product_id TEXT NOT NULL REFERENCES product_profiles(id),
    product_version INTEGER NOT NULL,
    evidence_json TEXT NOT NULL CHECK (json_valid(evidence_json))
);
CREATE TABLE knowledge_snapshots (
    id TEXT PRIMARY KEY,
    content_hash TEXT NOT NULL UNIQUE,
    payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
    created_at TEXT NOT NULL
);
CREATE TABLE speech_utterances (
    id TEXT PRIMARY KEY,
    cabinet_id TEXT NOT NULL REFERENCES cabinets(id),
    source_video_hash TEXT NOT NULL,
    audio_path TEXT NOT NULL,
    audio_sha256 TEXT NOT NULL,
    start_ms INTEGER NOT NULL CHECK (start_ms >= 0),
    end_ms INTEGER NOT NULL CHECK (end_ms >= start_ms),
    text TEXT NOT NULL,
    version INTEGER NOT NULL CHECK (version > 0),
    extractor TEXT NOT NULL,
    clock_json TEXT NOT NULL CHECK (json_valid(clock_json)),
    parsed_json TEXT NOT NULL CHECK (json_valid(parsed_json)),
    item_id TEXT REFERENCES items(id),
    link_status TEXT NOT NULL CHECK (link_status IN ('pending', 'linked')),
    body_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE task_commands (
    id TEXT PRIMARY KEY,
    cabinet_id TEXT NOT NULL REFERENCES cabinets(id),
    utterance_id TEXT NOT NULL REFERENCES speech_utterances(id),
    item_id TEXT REFERENCES items(id),
    kind TEXT NOT NULL CHECK (kind IN ('before_expiry', 'after_expiry_remove')),
    status TEXT NOT NULL CHECK (status IN ('needs_input', 'scheduled', 'active', 'canceled')),
    version INTEGER NOT NULL CHECK (version > 0),
    data_json TEXT NOT NULL CHECK (json_valid(data_json)),
    reminder_id TEXT REFERENCES reminders(id),
    body_hash TEXT NOT NULL,
    UNIQUE (utterance_id, kind)
);
CREATE TABLE item_event_rules (
    id TEXT PRIMARY KEY,
    command_id TEXT NOT NULL UNIQUE REFERENCES task_commands(id),
    item_id TEXT NOT NULL REFERENCES items(id),
    version INTEGER NOT NULL CHECK (version > 0),
    condition_json TEXT NOT NULL CHECK (json_valid(condition_json)),
    created_at TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1))
);
CREATE TABLE rule_firings (
    id TEXT PRIMARY KEY,
    rule_id TEXT NOT NULL REFERENCES item_event_rules(id),
    rule_version INTEGER NOT NULL,
    event_id TEXT NOT NULL REFERENCES verified_events(id),
    reminder_id TEXT NOT NULL REFERENCES reminders(id),
    UNIQUE (rule_id, rule_version, event_id)
);
CREATE INDEX products_scope ON product_profiles(cabinet_id, status, barcode);
CREATE INDEX speech_capture ON speech_utterances(cabinet_id, source_video_hash);
