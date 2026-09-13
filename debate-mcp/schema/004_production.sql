-- Modo producción: una decisión de plan puede seguir el ciclo completo
-- deliberar → ejecutar → revisar → mergear.
--
-- status 'executing': el plan fue aprobado (ruling yes/conditional) y el
-- ejecutor está trabajando o esperando ser lanzado. No recibe turnos de
-- cabeza (no está 'open'); el relay la vigila y, al terminar la ejecución,
-- abre la decisión de revisión del diff como una decisión normal.
-- production=true marca las decisiones que al aprobarse pasan a 'executing';
-- las revisiones de diff son decisiones normales (production=false).

ALTER TABLE decisions DROP CONSTRAINT IF EXISTS decisions_status_check;
ALTER TABLE decisions ADD CONSTRAINT decisions_status_check
    CHECK (status IN ('open', 'split', 'closed', 'executing'));

ALTER TABLE decisions ADD COLUMN production boolean NOT NULL DEFAULT false;
