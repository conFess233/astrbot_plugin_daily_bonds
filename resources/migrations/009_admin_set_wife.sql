-- Admin-selected wives may already belong to another owner. Regular draws
-- still exclude occupied characters in GameplayService._valid_candidates.
DROP INDEX uq_active_subject;
CREATE UNIQUE INDEX uq_active_subject ON relationships(scope_id,period_id,mode,subject_id)
  WHERE state='active' AND mode IN ('husband','member');
CREATE UNIQUE INDEX uq_active_wife_owner_subject ON relationships(scope_id,period_id,owner_id,subject_id)
  WHERE state='active' AND mode='wife';
