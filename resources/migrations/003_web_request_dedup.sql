CREATE TABLE web_request_dedup (
  actor_username TEXT NOT NULL,
  endpoint TEXT NOT NULL,
  request_id TEXT NOT NULL,
  request_hash TEXT NOT NULL CHECK(length(request_hash)=64),
  response_json TEXT NOT NULL CHECK(json_valid(response_json)),
  created_at INTEGER NOT NULL,
  PRIMARY KEY(actor_username, endpoint, request_id)
);
