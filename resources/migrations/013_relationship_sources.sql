ALTER TABLE relationships ADD COLUMN acquisition_kind TEXT NOT NULL DEFAULT 'draw'
  CHECK(acquisition_kind IN ('draw','gift','steal'));
ALTER TABLE relationships ADD COLUMN source_owner_id TEXT;

UPDATE relationships SET acquisition_kind='steal' WHERE slot_kind='steal';
-- 仅回填唯一可确认的前一段关系，不在同秒多次转移时猜测来源。
UPDATE relationships AS r SET
  acquisition_kind=(SELECT CASE p.end_reason WHEN 'gifted' THEN 'gift' ELSE 'steal' END
    FROM relationships p WHERE p.scope_id=r.scope_id AND p.period_id=r.period_id
      AND p.mode=r.mode AND p.snapshot_id=r.snapshot_id AND p.ended_at=r.acquired_at
      AND p.owner_id<>r.owner_id AND p.end_reason IN ('gifted','stolen')),
  source_owner_id=(SELECT p.owner_id FROM relationships p
    WHERE p.scope_id=r.scope_id AND p.period_id=r.period_id AND p.mode=r.mode
      AND p.snapshot_id=r.snapshot_id AND p.ended_at=r.acquired_at
      AND p.owner_id<>r.owner_id AND p.end_reason IN ('gifted','stolen'))
WHERE (SELECT COUNT(*) FROM relationships p
  WHERE p.scope_id=r.scope_id AND p.period_id=r.period_id AND p.mode=r.mode
    AND p.snapshot_id=r.snapshot_id AND p.ended_at=r.acquired_at
    AND p.owner_id<>r.owner_id AND p.end_reason IN ('gifted','stolen'))=1;

UPDATE relationships AS r SET acquisition_kind='gift', source_owner_id=(
  SELECT o.actor_id FROM operation_log o WHERE o.result_code='GIFTED'
    AND json_extract(o.data_json,'$.new_relationship_id')=r.id LIMIT 1)
WHERE EXISTS(SELECT 1 FROM operation_log o WHERE o.result_code='GIFTED'
  AND json_extract(o.data_json,'$.new_relationship_id')=r.id);
UPDATE relationships AS r SET acquisition_kind='gift', source_owner_id=(
  SELECT i.sender_id FROM gift_invites i WHERE i.result_relationship_id=r.id LIMIT 1)
WHERE EXISTS(SELECT 1 FROM gift_invites i WHERE i.result_relationship_id=r.id);
