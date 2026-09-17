-- Phase 8: retain immutable export intentions without automatic legacy writes.
CREATE TABLE compatibility_policy(
 id INTEGER PRIMARY KEY CHECK(id=1),
 mode TEXT NOT NULL CHECK(mode IN ('automatic','on-demand','maintenance')),
 changed_at TEXT NOT NULL
);
INSERT INTO compatibility_policy VALUES(1,'automatic',strftime('%Y-%m-%dT%H:%M:%SZ','now'));
CREATE TABLE compatibility_delivery_schedule(
 operation_id TEXT NOT NULL, ordinal INTEGER NOT NULL,
 mode TEXT NOT NULL CHECK(mode IN ('automatic','on-demand')),
 previous_operation_id TEXT, previous_ordinal INTEGER,
 PRIMARY KEY(operation_id,ordinal),
 FOREIGN KEY(operation_id,ordinal) REFERENCES compatibility_deliveries(operation_id,ordinal),
 FOREIGN KEY(previous_operation_id,previous_ordinal) REFERENCES compatibility_deliveries(operation_id,ordinal),
 CHECK((previous_operation_id IS NULL)=(previous_ordinal IS NULL))
);
CREATE TRIGGER immutable_delivery_schedule_update BEFORE UPDATE ON compatibility_delivery_schedule BEGIN SELECT RAISE(ABORT,'immutable delivery schedule'); END;
CREATE TRIGGER immutable_delivery_schedule_delete BEFORE DELETE ON compatibility_delivery_schedule BEGIN SELECT RAISE(ABORT,'immutable delivery schedule'); END;
