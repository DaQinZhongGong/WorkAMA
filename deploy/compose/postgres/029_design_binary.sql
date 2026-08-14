-- AMA-Design controlled binary artifacts and immutable provenance.
ALTER TABLE ag_design_asset ADD COLUMN IF NOT EXISTS content_bytes BYTEA;

ALTER TABLE ag_design_job DROP CONSTRAINT IF EXISTS ag_design_job_output_format_check;
ALTER TABLE ag_design_job
  ADD CONSTRAINT ag_design_job_output_format_check
  CHECK (output_format IN ('svg','png','jpeg','json'));

CREATE OR REPLACE FUNCTION ag_design_asset_provenance_immutable()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.provenance IS DISTINCT FROM OLD.provenance
     OR NEW.provenance_hash IS DISTINCT FROM OLD.provenance_hash
     OR NEW.parent_asset_ids IS DISTINCT FROM OLD.parent_asset_ids
     OR NEW.content_bytes IS DISTINCT FROM OLD.content_bytes
     OR NEW.content_sha256 IS DISTINCT FROM OLD.content_sha256
     OR NEW.size_bytes IS DISTINCT FROM OLD.size_bytes
     OR NEW.content_type IS DISTINCT FROM OLD.content_type
     OR NEW.artifact_ref IS DISTINCT FROM OLD.artifact_ref THEN
    RAISE EXCEPTION 'design asset content and provenance are immutable';
  END IF;
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS ag_design_asset_provenance_immutable_trigger ON ag_design_asset;
CREATE TRIGGER ag_design_asset_provenance_immutable_trigger
BEFORE UPDATE ON ag_design_asset
FOR EACH ROW EXECUTE FUNCTION ag_design_asset_provenance_immutable();
