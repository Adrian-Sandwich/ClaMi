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

## Paso 3: recuperación semántica

Combinar búsqueda textual indexada con similitud semántica y relaciones del grafo.
La implementación actual recorre nodos y compara términos: no comprende sinónimos
y su costo crece con el grafo. Comparar contra casos de recuperación anotados antes
de sustituirla; exigir fuentes relevantes y aislamiento por proyecto.

## Paso 4: síntesis conjunta

Producir una respuesta breve revisada por las tres cabezas, distinguiendo acuerdos,
desacuerdos y preguntas pendientes. Unanimidad de etiquetas INFO no demuestra
consenso sobre el contenido ni certeza. Evaluar convergencia dentro de un presupuesto
de ciclos; conservar el journal como detalle consultable.

## Paso 5: aprender de resultados

Relacionar decisiones con implementaciones, pruebas y correcciones posteriores.
Registrar qué funcionó y bajo qué condiciones, incluyendo fallos y evidencia que
contradiga una conclusión anterior. Reutilizar aprendizajes con alcance explícito.
Esto mejora la memoria del sistema; no reentrena por sí mismo los modelos.

## Paso 6: medir la mejora

Mantener casos de evaluación de continuidad, recuperación, contradicciones,
síntesis y utilidad. Medir aciertos, contaminación entre proyectos, latencia y
costo. Promover cambios sólo si mejoran esos resultados frente a la versión anterior.
