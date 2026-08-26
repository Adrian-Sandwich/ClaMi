-- Canal global para el relay.
--
-- El relay tenía que enterarse de mensajes en CUALQUIER thread, y como los
-- canales de notify son por thread ('debate_<thread>'), no podía hacer LISTEN
-- sin saber de antemano la lista — así que hacía polling cada 20s contra una
-- base que ya sabía empujar eventos. Con un canal fijo adicional el relay
-- hace un solo LISTEN y despierta al instante.
--
-- Se mantiene el notify por thread: es el que usa wait_messages() de los
-- agentes, que sí sabe sobre qué thread está esperando.

CREATE OR REPLACE FUNCTION notify_message() RETURNS trigger AS $$
BEGIN
    PERFORM pg_notify('debate_' || NEW.thread, NEW.id::text);
    PERFORM pg_notify('debate_all', NEW.id::text);
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
