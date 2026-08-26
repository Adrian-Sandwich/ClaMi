-- Esquema base del tablero de debate.
--
-- Escrito a partir del DDL que ya existía en la DB `trade_debate` (creada a
-- mano en su momento). Todo es idempotente a propósito: aplicar esta
-- migración sobre la base viva tiene que ser un no-op, y sobre una base
-- vacía tiene que reconstruir el tablero entero.

CREATE TABLE IF NOT EXISTS messages (
    id         bigserial PRIMARY KEY,
    thread     text NOT NULL,
    author     text NOT NULL,
    kind       text NOT NULL DEFAULT 'analisis',
    body       text NOT NULL,
    artifact   text,
    created_at timestamptz NOT NULL DEFAULT now()
);

-- read_thread() filtra por (thread, id > since_id) y ordena por id: este
-- índice cubre el predicado y el orden de una sola pasada.
CREATE INDEX IF NOT EXISTS messages_thread_id ON messages (thread, id);

-- El fan-out en tiempo real de wait_messages(): cada INSERT despierta a los
-- listeners del canal del thread sin que nadie tenga que hacer polling.
--
-- OJO: el nombre de canal se trunca a 63 bytes (NAMEDATALEN-1). server.py
-- valida el largo del thread antes de insertar; si esa validación se saltea,
-- el pg_notify de acá hace fallar el INSERT completo.
CREATE OR REPLACE FUNCTION notify_message() RETURNS trigger AS $$
BEGIN
    PERFORM pg_notify('debate_' || NEW.thread, NEW.id::text);
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS messages_notify ON messages;
CREATE TRIGGER messages_notify
    AFTER INSERT ON messages
    FOR EACH ROW EXECUTE FUNCTION notify_message();
