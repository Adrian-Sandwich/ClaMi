CREATE TABLE decision_outcomes (
    id bigserial PRIMARY KEY,
    decision_id bigint NOT NULL REFERENCES decisions(id) ON DELETE CASCADE,
    request_id text NOT NULL UNIQUE,
    status text NOT NULL CHECK (status IN ('worked','failed','partial','unknown')),
    observation text NOT NULL,
    evidence text NOT NULL,
    lesson text NOT NULL DEFAULT '',
    message_id bigint NOT NULL REFERENCES messages(id),
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX decision_outcomes_decision ON decision_outcomes(decision_id,id);
