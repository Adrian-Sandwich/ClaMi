-- Motor de decisiones MAGI: la Decisión pasa a ser la unidad de trabajo.
--
-- decisions: una fila por pregunta/artefacto sometido a las cabezas. El
-- journal del debate sigue siendo la tabla messages (thread = decisions.thread),
-- así el tablero queda auditable y memory-graph sigue ingiriendo de la misma
-- fuente. positions es el contrato estructurado: el voto es dato, el
-- razonamiento es historia (el body del message linkeado por message_id).
--
-- ruling usa el vocabulario de TomaszRewak/MAGI: yes/no/conditional/info/error.
-- status 'split' = las cabezas no se pusieron de acuerdo: el sistema no inventa
-- consenso, queda a la espera del arbitraje humano (kind='arbitraje').

CREATE TABLE IF NOT EXISTS decisions (
    id              bigserial PRIMARY KEY,
    title           text NOT NULL,
    artifact        text,
    protocol        text NOT NULL DEFAULT 'vote'
                    CHECK (protocol IN ('vote', 'critique', 'adaptive')),
    status          text NOT NULL DEFAULT 'open'
                    CHECK (status IN ('open', 'split', 'closed')),
    ruling          text CHECK (ruling IN ('yes', 'no', 'conditional', 'info', 'error')),
    confidence      real,
    minority_report jsonb,
    thread          text NOT NULL UNIQUE,
    heads           jsonb NOT NULL,        -- asientos participantes (snapshot al abrir)
    anchor_id       bigint,                -- id del message 'analisis' inicial del journal
    round           int  NOT NULL DEFAULT 1,
    created_by      text NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    closed_at       timestamptz
);

CREATE INDEX IF NOT EXISTS decisions_status ON decisions (status);

CREATE TABLE IF NOT EXISTS positions (
    decision_id bigint NOT NULL REFERENCES decisions (id),
    head        text   NOT NULL,
    round       int    NOT NULL,
    position    text   NOT NULL
                CHECK (position IN ('yes', 'no', 'conditional', 'info')),
    conditions  jsonb,
    message_id  bigint,                    -- message 'posicion' del journal
    created_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (decision_id, head, round)
);

CREATE INDEX IF NOT EXISTS positions_round ON positions (decision_id, round);

-- Canal del relay: despierta cuando se abre una decisión (INSERT) o cuando el
-- motor cambia de ronda/estado (UPDATE), así las cabezas del turno siguiente
-- se disparan al instante aunque nadie haya posteado un message nuevo.
CREATE OR REPLACE FUNCTION notify_decision() RETURNS trigger AS $$
BEGIN
    PERFORM pg_notify('decision_all', NEW.id::text);
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS decisions_notify ON decisions;
CREATE TRIGGER decisions_notify
    AFTER INSERT OR UPDATE OF round, status ON decisions
    FOR EACH ROW EXECUTE FUNCTION notify_decision();
