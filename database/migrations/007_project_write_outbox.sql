-- DB-first writes: business changes and durable compatibility deliveries commit together.
CREATE TABLE project_write_requests(
 operation_id TEXT PRIMARY KEY NOT NULL REFERENCES sync_runs(id),
 version TEXT NOT NULL, request_json TEXT NOT NULL CHECK(json_valid(request_json)),
 db_committed_at TEXT NOT NULL
);
CREATE TABLE compatibility_deliveries(
 operation_id TEXT NOT NULL REFERENCES project_write_requests(operation_id),
 ordinal INTEGER NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('file','data-table')),
 target TEXT NOT NULL, payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),
 payload_hash TEXT NOT NULL, previous_hash TEXT,
 status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','complete')),
 completed_at TEXT, PRIMARY KEY(operation_id,ordinal)
);
CREATE TRIGGER immutable_project_write_request_update BEFORE UPDATE ON project_write_requests BEGIN SELECT RAISE(ABORT,'immutable write request'); END;
CREATE TRIGGER immutable_project_write_request_delete BEFORE DELETE ON project_write_requests BEGIN SELECT RAISE(ABORT,'immutable write request'); END;
CREATE TRIGGER immutable_compatibility_payload BEFORE UPDATE OF operation_id,ordinal,kind,target,payload_json,payload_hash,previous_hash ON compatibility_deliveries BEGIN SELECT RAISE(ABORT,'immutable compatibility delivery'); END;
CREATE TRIGGER immutable_compatibility_delivery_delete BEFORE DELETE ON compatibility_deliveries BEGIN SELECT RAISE(ABORT,'immutable compatibility delivery'); END;
