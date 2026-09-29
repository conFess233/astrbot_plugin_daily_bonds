ALTER TABLE relationships
  ADD COLUMN slot_kind TEXT NOT NULL DEFAULT 'normal'
  CHECK(slot_kind IN ('normal','steal'));

CREATE INDEX ix_owner_active_slot
  ON relationships(scope_id,period_id,mode,owner_id,slot_kind,state);
