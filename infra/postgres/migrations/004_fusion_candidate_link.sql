-- Attribution owns spill IDs; retain a separate link to the model candidate.
ALTER TABLE spill_incidents ADD COLUMN IF NOT EXISTS candidate_id UUID;
UPDATE spill_incidents i SET candidate_id=c.candidate_id
FROM spill_candidates c WHERE i.id=c.candidate_id AND i.candidate_id IS NULL;
CREATE INDEX IF NOT EXISTS idx_incident_candidate ON spill_incidents(candidate_id);
