-- Source claims are distinct from usable body verification.
ALTER TABLE content_fetch_attempts ADD COLUMN body_integrity TEXT NOT NULL DEFAULT 'unassessed' CHECK(body_integrity IN ('unassessed','consistent','no_body','held_missing_body','held_hash_mismatch'));
ALTER TABLE content_fetch_attempts ADD COLUMN source_status TEXT;
ALTER TABLE content_fetch_attempts ADD COLUMN stored_body_hash TEXT;
ALTER TABLE content_fetch_attempts ADD COLUMN stored_body_length INTEGER;
ALTER TABLE content_fetch_attempts ADD COLUMN source_content_path TEXT;
ALTER TABLE content_fetch_attempts ADD COLUMN computed_body_hash TEXT;
CREATE TRIGGER held_body_insert BEFORE INSERT ON content_fetch_attempts WHEN NEW.body_integrity IN ('held_missing_body','held_hash_mismatch') AND (NEW.version_id IS NOT NULL OR NEW.status='verified') BEGIN SELECT RAISE(ABORT,'held body cannot claim verified payload'); END;
CREATE TRIGGER held_body_update BEFORE UPDATE ON content_fetch_attempts WHEN NEW.body_integrity IN ('held_missing_body','held_hash_mismatch') AND (NEW.version_id IS NOT NULL OR NEW.status='verified') BEGIN SELECT RAISE(ABORT,'held body cannot claim verified payload'); END;
