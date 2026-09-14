# Evolución de la memoria MAGI

## Paso 1: recuperar contexto útil (implementado)

La ingesta de decisiones conserva el objetivo, repositorio, estado, condiciones
registradas y el último mensaje relevante de cada autor (hasta 1800 caracteres),
con su identificador y fecha. Son fuentes históricas, no hechos verificados.

La recuperación prioriza el mismo thread, después el repositorio y la coincidencia
de términos. Usa fechas del contenido y evita el antiguo relleno de decisiones
recientes sin relación. Recorre un salto del grafo hacia archivos, código y documentos.
El contexto enviado tiene un máximo de 6500 caracteres y llega al consejo y al chat.
El chat busca por la última intervención humana; todavía no pasa un ámbito de
repositorio o thread al recuperador.

Validación: pruebas de relevancia, identidad de repositorios, continuidad,
referencias a mensajes, recorrido de relaciones y presupuesto de contexto.

## Paso 2: actualización continua y memoria estructurada (implementado)

El relay ejecuta la ingesta de conversaciones en segundo plano al arrancar y cada
30 segundos después de terminar la anterior. Un fallo se reintenta con espera
creciente (hasta 240 segundos); cada proceso tiene un timeout de 60 segundos.
El heartbeat y `healthcheck.py` muestran el estado y la última sincronización
exitosa. Esto actualiza conversaciones y decisiones; las sesiones externas,
documentación, código y exportación 3D siguen dependiendo del refresh existente.

Se consulta el historial completo de Postgres, pero sólo se reescriben nodos cuyo
contenido cambió. Los checkpoints se confirman junto con los nodos en SQLite.
Un reinicio o fallo no pierde cambios pendientes. No es aún una consulta incremental
por eventos: reducir el volumen leído queda pendiente para historiales grandes.

Las declaraciones humanas explícitas se guardan por conversación con fuente y
versiones. Por ejemplo:

```text
Objetivo: Publicar una versión estable
Restricción [datos]: Conservar los datos existentes
Pendiente [pruebas]: Validar la migración
```

Otra declaración del mismo tipo y clave sustituye la versión vigente, preservando
el historial. Sin clave se usa `general`; claves distintas conservan elementos
independientes. `Pendiente [pruebas]: resuelto` cierra ese pendiente; `cancelado`
también desactiva un elemento. Se ignoran declaraciones de modelos, citas y bloques
de código. El texto libre se conserva como contexto con su fuente, pero no se
convierte automáticamente en preferencias o restricciones permanentes.

## Paso 3: recuperación semántica (implementado, evaluación inicial)

La búsqueda combina coincidencias de palabras con embeddings multilingües locales
de `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`, mediante
FastEmbed 0.8.0. Consulta los mismos nodos y conserva las prioridades de conversación
y repositorio, las referencias a fuentes y el recorrido acotado del grafo.
La similitud se aplica a decisiones, conversaciones y documentos. Los archivos y
símbolos de código se recuperan por palabras o relaciones; sus rutas solas producen
demasiadas asociaciones semánticas débiles. No envía textos a un servicio de
embeddings. La similitud no mide veracidad.

Instalación en Windows (desde la raíz):

```powershell
.\debate-mcp\.venv\Scripts\python.exe -m pip install -r debate-mcp/requirements-semantic.txt
.\debate-mcp\.venv\Scripts\python.exe debate-mcp/semantic_memory.py --download
```

El modelo se descarga explícitamente una vez a `memory-graph/models/` (ignorado por
Git). La consulta sólo usa archivos locales. Si falta el modelo, la dependencia
o el índice, sigue disponible la recuperación por palabras. `MEMORY_SEMANTIC=0`
desactiva la similitud en consultas.

El relay actualiza el índice después de sincronizar conversaciones. Los vectores
se guardan en SQLite por modelo y contenido; las filas obsoletas no participan
hasta reindexarse. Los lotes confirmados sobreviven a un reinicio. El estado
`semantic_status` aparece en el heartbeat y los fallos en `healthcheck.py`.

Evaluación reproducible con el modelo instalado:

```powershell
$env:CLAMI_SEMANTIC_EVAL = '1'
.\debate-mcp\.venv\Scripts\python.exe -m pytest tests/test_semantic_memory.py -s
```

En seis reformulaciones de desarrollo, la búsqueda por palabras obtuvo 0/6
primeros resultados correctos y la híbrida 5/6 con umbral de coseno 0.45. Una
consulta ajena (receta de pastel) no recuperó resultados. El umbral se ajustó con
esos mismos ejemplos: no son una evaluación independiente ni una garantía general.
El caso de autenticación sigue fallando. Ampliar el conjunto con casos reales,
negaciones, sinónimos y consultas sin respuesta antes de ajustar más el ranking.

La comparación sigue siendo exhaustiva y crece con el número de vectores. Quedan
pendientes un índice textual FTS y búsqueda vectorial aproximada para grandes
historiales. Los pasajes se limitan a 24 por nodo; los documentos largos pueden
perder cobertura. Las fuentes sin repositorio declarado no pueden aislarse por
proyecto con la misma garantía que las decisiones que sí lo especifican.

Referencia del proveedor: https://qdrant.github.io/fastembed/examples/Supported_Models/

## Paso 4: síntesis conjunta (implementado)

El relay prepara una respuesta conjunta para el dossier cerrado, dividido o en
ejecución que recibió actividad más recientemente. No procesa automáticamente todo
el historial. Un asiento redacta y cada asiento esperado revisa la fidelidad de
la síntesis a las fuentes. Puede corregirse y revisarse una segunda vez: máximo
dos ciclos, ocho invocaciones con tres asientos, 120 segundos por invocación.
El borrador se publica antes de completar las revisiones, con la cabeza y fase
activas. Un timeout o respuesta inválida no cuenta como objeción editorial y no
provoca por sí solo otro ciclo. Si falla la corrección, se conserva el borrador
anterior como parcial. El caso #26 motivó estas pruebas de progreso y recuperación.

La interfaz presenta respuesta, puntos compartidos, diferencias y preguntas
pendientes. Un borrador que no obtuvo todas las revisiones favorables se marca
como parcial y muestra las objeciones o revisiones que no pudieron completarse.
Validar la fidelidad editorial no significa compartir las otras posturas. El
porcentaje de votos se etiqueta como acuerdo de voto, no certeza factual.
Los triángulos permanecen; los registros y aportes completos son detalle opcional.

La síntesis se guarda en el dossier, con fuentes, revisiones y versión del journal.
Si cambia el contexto o la ronda durante la redacción, no se publica el resultado
obsoleto. Un candado de Postgres impide trabajo duplicado entre relays. Reiniciar
recupera un trabajo interrumpido; un resultado parcial o fallido no se reintenta
indefinidamente sin nuevo contexto. El proceso editorial no modifica votos,
aprobaciones ni la ejecución de producción.

Límite actual: se incluyen las posiciones de la última ronda (hasta 3500 caracteres
por aporte) y tres intervenciones humanas recientes. La síntesis puede omitir
matices fuera de ese contexto. La convergencia semántica del debate original sigue
pendiente: este paso revisa la respuesta final, no sustituye el motor de rondas.

## Paso 5: aprender de resultados (implementado)

La opción **¿Cómo salió?** permite registrar funcionó, falló, parcial o sin confirmar,
con observación, evidencia indicada y un aprendizaje opcional. Se conserva en la
misma decisión sin reabrirla ni autorizar ejecuciones. Un identificador por envío
evita duplicados al reintentar un fallo de conexión. Las correcciones son nuevos
reportes: el historial no se sobrescribe.

La migración `005_outcomes.sql` guarda los reportes y su mensaje de origen en
Postgres. La sincronización los lleva al grafo y la búsqueda textual/semántica
puede recuperar observaciones y aprendizajes. El contexto identifica el último
reporte, señala resultados anteriores diferentes y limita el aprendizaje al caso.
Los reportes del usuario no se presentan como verificación independiente.

También se conservan eventos del journal emitidos por MAGI: ejecución fallida,
merge bloqueado o completado, con referencia al mensaje; los metadatos disponibles
enlazan revisión y commits. Un merge no implica pruebas exitosas ni utilidad.
No se deducen causas de fallos automáticamente: el detalle registrado y la evidencia
siguen siendo necesarios. Esto mejora la memoria; no reentrena los modelos ni
demuestra por sí solo una mejora global en la calidad de sus respuestas.

## Paso 6: medir la mejora

Mantener casos de evaluación de continuidad, recuperación, contradicciones,
síntesis y utilidad. Medir aciertos, contaminación entre proyectos, latencia y
costo. Promover cambios sólo si mejoran esos resultados frente a la versión anterior.
