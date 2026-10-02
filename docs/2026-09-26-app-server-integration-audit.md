# Auditoría ReciApp iOS ↔ servidor

Fecha: 2026-09-26
Repositorios revisados: `ReciApp-iOS` y `ReciApp-Server`.

Último rollout de producción verificado: 2026-09-27. Ese día API, worker, RLS y migraciones 008–011 pasaron las comprobaciones descritas abajo. La clave APNs y Team ID se validaron con Apple; Coolify y entrega en iPhone quedaron pendientes. La integración iOS estaba en `main` (`864a487`), sin distribución.

Revalidación: 2026-10-02. Se añadieron recuperación Pro, sincronización cloud de biblioteca y fixes APNs; cambios locales no prueban distribución ni despliegue. La prueba más reciente aplicó migraciones 001–013 en PostgreSQL 14 temporal. Producción no se pudo verificar desde este host: DNS no resolvió el dominio. No usar respuestas 200 del 27-Sep como prueba del estado actual; véase el historial al final.

## Resultado

En las fuentes locales, los contratos principales de login, perfil, biblioteca, detalle, importación, cola, traducción, borrado y errores coinciden entre Swift y FastAPI. La última prueba de producción exitosa, del 2026-09-27, confirmó API y worker con el mismo build, `WORKER_ENABLED=true`, migraciones 008–011 aplicadas y smoke test RLS desde la conexión real de API. Es evidencia histórica, no una comprobación del despliegue actual. La integración de restauración Pro añadida el 2026-10-01 también necesita despliegue y prueba real.

## Cambios hechos en esta revisión

- `GET /v1/me` ahora devuelve `free_used_this_year` además del campo histórico `free_used_this_week`. Ambos representan el mismo contador anual durante la transición.
- El mensaje de límite preventivo de la app ahora dice “por año”, que coincide con la cuota real de tres importaciones nuevas al año UTC.
- La renovación del refresh token ahora revoca el token anterior e inserta el siguiente en una única sentencia transaccional. Antes eran operaciones separadas: si fallaba el insert después de revocar, se podía dejar al usuario sin token válido.
- La app persiste un `request_id` en Keychain antes de renovar. Si se pierde la respuesta tras rotar el token, reintenta con el mismo ID y servidor devuelve el mismo sucesor durante 15 minutos. Antes ese caso podía cerrar la sesión y forzar login con Apple. Logout revoca también el sucesor desconocido; replay exige sucesor aún activo para no reabrir sesión tras logout. La migración 009 añade solo columnas opcionales; `/ready` ahora las exige.
- El borrado de cuenta limpia biblioteca, snapshot cloud, trabajos y accesos compartidos, ledger y eventos de seguridad, revoca sesiones y cierra el perfil en una sola transacción. Antes se ignoraban errores de limpieza y se podía responder éxito dejando datos ligados a la cuenta. Un fallo DB ahora responde `503 ACCOUNT_DELETION_UNAVAILABLE`; el cliente conserva la sesión para reintentar. Cada URL de trabajo se anonimiza con un valor distinto por ID, para no chocar con el índice único de imports activos. También cancela trabajos pendientes y `save_user_recipe` ya no puede volver a vincular una receta si un worker termina tras cerrar la cuenta.
- El cliente Pro ahora espera confirmación `is_pro` de `/v1/me` antes de reanudar imports tras compra/restauración. El refresh distingue confirmación, falta de confirmación tras reintentos y cancelación/cambio de cuenta; una petición obsoleta ya no muestra falso error ni reanuda cola de otra sesión.
- Los errores transitorios de endpoints de autenticación y perfil ahora incluyen `detail.code/message` además del sobre superior histórico. El cliente Swift compartía el parser para ambos formatos y antes solo entendía el campo `detail`.
- Los trabajos fallidos ahora incluyen un `error_code` estable. iOS ofrece reintento manual para `extraction_retryable` y `stale_job`, con un nuevo ID de entrega; errores permanentes como `link_in_bio` no reintentan en bucle.
- Un fallo temporal al adjuntar una receta cacheada o recién completada a la biblioteca ahora devuelve `503` con `Retry-After`. Repetir el mismo ID de entrega o sondear el mismo job vuelve a intentar el enlace, en vez de responder éxito dejando la receta invisible.
- Endurecí `/ready`: no basta con que PostgreSQL responda. Comprueba que `client_delivery_id` sea UUID y que el índice de idempotencia exista, sea único, válido y listo, y use las columnas y predicado correctos. Así una migración parcial o un índice inválido con el mismo nombre no anuncia que el backend puede aceptar imports.
- El aviso de receta lista está traducido a los 46 idiomas no ingleses del catálogo. En esta revisión también traduje el permiso, el estado desactivado y los ajustes para esos 46 idiomas; antes esos cuatro textos tenían solo español y catalán. Sigue siendo una notificación local de mejor esfuerzo.
- Mejoré los eventos de diagnóstico de cuota y límites: ahora guardan el código concreto (`FREE_YEARLY_LIMIT` o `PRO_FAIR_USE_LIMIT`), UUID interno de cuenta en los rechazos por usuario, límite/ventana y `X-Correlation-ID`; no guardan URLs ni cuerpos. Añadí cobertura para ambos límites Pro/Free y el rate limit por usuario.
- Actualicé las pruebas del servidor que apuntaban a APIs antiguas del extractor/STT, límites y precios anteriores; mantienen la intención de comprobar los casos actuales.
- Añadidas pruebas unitarias para la rotación atómica del refresh token y el contador anual de perfil.
- La suite completa del servidor pasa con 254 tests y 2 skips. Añadí contratos cruzados que verifican rutas iOS contra FastAPI, `request_id` en refresh/logout y cobertura de los cuatro controles de notificación en todos los idiomas de aviso final. `ClientStateHarness` pasa 48 tests. En PostgreSQL temporal apliqué migraciones 001–009: refresh concurrente devolvió el mismo sucesor, expiración/logout/perfil cerrado rechazaron replay, cliente legacy conservó rotación de un uso, endpoints HTTP refresh/logout pasaron y readiness falló al quitar cada columna 009. El clúster temporal se eliminó. Migraciones 008 y 009 siguen pendientes en producción; desplegar código sin ambas hará que `/ready` falle y no completa imports/refresh replay.
- Añadí cobertura iOS para respuestas HTTP 500/502: conserva el import y permite recuperarlo sin repetir automáticamente el `POST` incierto.
- ShareInbox guardaba la entrega, pero repetía errores transitorios del extractor cada 2 segundos. Ahora aplica backoff exponencial hasta 60 segundos, respeta `Retry-After` cuando fija un mínimo y limpia el estado al completar o reintentar manualmente. Verificado con 48 pruebas del `ClientStateHarness` y build iOS Simulator.
- Añadí notificaciones APNs de finalización con outbox durable para extracción y traducción, limpieza por logout y borrado de cuenta, reintentos acotados, detección de token inválido y heartbeat de worker. Cache hits no generan push. Registro push solo se anuncia si credenciales APNs y worker durable están listos; si no, la app usa aviso local. La alerta usa el idioma seleccionado en ReciApp y cubre los 50 códigos compartidos con el servidor. Generador: `scripts/sync_apns_localizations.py`.
- La revisión de la migración 010 detectó un constraint PostgreSQL inválido para el rango `{32,512}` en una expresión regular. Lo reemplacé por comprobación de longitud más regex hexadecimal. Migraciones 001–010 pasan en PostgreSQL 14 temporal; una transacción semántica confirmó aviso para dueño y usuario compartido, exclusión de cache hit, claim duradero, idioma, logout y eliminación de entregas. Readiness real pasó sin worker requerido y con heartbeat fresco; rechazó heartbeat obsoleto.
- Cobertura actual del servidor: 265 tests pasan y 2 omitidos. Build iOS Debug y Release para Simulator pasan; `ClientStateHarness` conserva 48 tests pasados. Las builds no verifican firma APNs ni entrega en dispositivo.
- Corregí una ventana de pérdida al aceptar un job desde ShareInbox: la app persistía el `job_id` antes de confirmar la entrega local. Si iOS cierra la app tras recibir la respuesta del servidor, el job se puede reanudar; si se cierra antes, el mismo `client_delivery_id` permite repetir el envío de forma idempotente. Añadí un contrato cruzado que protege el orden.
- Alineé `ReciApp-iOS/INTEGRACION_SWIFT.md` y su checklist con el contrato vigente: 50 idiomas, tres imports Free por año UTC, códigos `FREE_YEARLY_LIMIT`/`SPEND_LIMIT`, `client_delivery_id` y `error_code`. Eliminé instrucciones antiguas que recomendaban una cuota incorrecta y aclaré que cliente depende de migración 008 para dedupe, 009 para replay de refresh.
- En la relectura completa encontré que el README del servidor aún decía “1 recipe / week” y listaba modelos OpenAI obsoletos; corregí cuota y modelos según `app/config.py`. También actualicé el checklist iOS↔servidor: ahora refleja los 7 webhooks Superwall procesados observados y deja explícita la prueba sandbox pendiente. Quité una referencia contradictoria a “0 active endpoints” que no era evidencia actual.
- Corregí la purga local al borrar una cuenta: antes quedaba la lista de compra en `UserDefaults` y podían reaparecer enlaces ShareInbox pendientes del usuario en el App Group. Ahora se eliminan con las cachés de recetas/carpetas y los jobs locales, conservando entregas sin propietario u otras cuentas; añadí cobertura cruzada.
- Encontré que el progreso del modo cocina se guardaba solo por receta, compartido entre cuentas, y no se borraba al eliminar una cuenta. Ahora la clave incluye usuario y receta, la demo sin cuenta usa un ámbito separado, y el borrado purga solo el ámbito de esa cuenta. Las claves legacy sin dueño se eliminan al primer acceso nuevo porque no se pueden atribuir con seguridad; añadí prueba que usa la política real compartida por el harness.
- La configuración de producción versionada arrancaba solo FastAPI con `WORKER_ENABLED=false`, así que el procesamiento dependía de `BackgroundTasks` en memoria. Preparé un servicio `recipe-worker` que reclama trabajos durables de PostgreSQL y activa el modo worker también en el API. Está en `1de66d1` y su runbook actualizado en `950cf1d`, rama local `codex/reciapp-durable-worker`; el repo de infraestructura no tiene remoto Git configurado, por lo que no puedo publicarla. No está desplegado; falta confirmar esquema y hacer rollout controlado.

## Contratos comprobados

- Apple login, refresh y logout: campos `identity_token`, `nonce`, `full_name`, `authorization_code`, `refresh_token` y respuesta de sesión compatibles.
- Perfil: `/v1/me`; biblioteca: `/v1/me/recipes`; detalle: `/v1/recipes/{id}`.
- Importación: `POST /v1/extract`; seguimiento: `/v1/me/jobs` y `/v1/jobs/{id}`; la app persiste y sigue `next_job_id`.
- Borrado de receta y cuenta: `DELETE /v1/me/recipes/{id}` y `DELETE /v1/me`.
- El borrado de cuenta es idempotente y atómico dentro de PostgreSQL; mantiene `usage_events` para preservar cuota anual tras reactivar la misma identidad Apple.
- La revocación del refresh token de Apple durante el borrado es de mejor esfuerzo. Si Apple no responde o falla la clave local, el perfil y los datos de la app se cierran igualmente; el token cifrado puede quedar almacenado para revisión operativa. No se debe describir esta ruta como confirmación de borrado total en Apple.
- El cliente respeta `Retry-After`, distingue 401/403/404/422/429/503 y evita repetir automáticamente un POST de extracción cuyo resultado sea incierto.
- Carpetas, asignación de recetas, favoritos, etiquetas personales, preferencias de presentación, lista de compra, unidades/temperatura y progreso de cocción se guardan localmente por UUID y se sincronizan con `/v1/me/library-state` usando revisiones CAS. Conflictos conservan versiones previas para recuperación; la biblioteca de recetas sigue en rutas separadas. El borrado de cuenta elimina también el snapshot cloud; su sincronización concurrente se serializa con el bloqueo del perfil. Verificación PostgreSQL temporal 001–013; estado de migraciones productivas 012–013 y sincronización en dos dispositivos siguen sin comprobar.
- El identificador de entrega `client_delivery_id` del cliente requiere la columna creada por `migrations/008_extract_delivery_idempotency.sql`.
- Pro depende de Superwall: iOS identifica al usuario con su UUID y FastAPI valida `Svix` en `/v1/webhooks/superwall`. Una consulta de producción histórica registró recepción de eventos y secreto configurado; esto no confirma estado actual. Falta compra/restauración sandbox controlada seguida del `GET /v1/me` de esa cuenta.

## Riesgos de la captura inicial del 2026-09-26

Los apartados siguientes describen el estado observado antes del rollout del 2026-09-27. No representan el estado posterior; consultar “Rollout de producción completado” y la revalidación del 2026-10-01.

### Migración 008 de imports: falta en producción

Observaciones de solo lectura en producción, 2026-09-26:

- Reconsulta pública actual: `/health` HTTP 200 (`status=ok`) y `/ready` HTTP 200 (`status=ready`, `environment=production`, `maintenance=false`, latencia 2 ms). La respuesta no informa versión ni qué esquema valida.
- Confirmé que `/openapi.json` y `/docs` devuelven 404 en producción; FastAPI desactiva la documentación en ese entorno. No se puede comparar el contrato publicado desde OpenAPI, así que la evidencia del despliegue queda limitada a probes, introspección SQL y requests agregadas.
- En la inspección anterior, la API desplegada aceptaba `client_delivery_id` y `OPENAI_API_KEY` estaba configurada; no revalidé esas dos condiciones en el probe actual.
- La introspección PostgreSQL directa de esta reconsulta devolvió `false/false`: siguen ausentes `extract_jobs.client_delivery_id` y `extract_jobs_user_delivery_unique`.
- Conteo de la ventana reciente de 24 h: 965 registros; no aparecen marcadores `request completed`, etapas/fallos de extracción, errores upstream, rate limits ni líneas `ERROR`/`WARNING`. Al faltar marcadores de request, los logs no permiten estimar tráfico ni tasas 4xx/5xx; cero coincidencias no prueba que no hubiera solicitudes.
- `api_request_logs`, fuente agregada más útil que stdout: 108 requests en 24 h, 54 respuestas 2xx, 54 4xx y 0 5xx. `/v1/extract`: 28 requests, todas fallidas (10 HTTP 403, 18 HTTP 429); perfil 15/5, biblioteca 10/5, cola 26/1 (2xx/4xx), detalle 1/0. No hay jobs pendientes, procesándose, completados ni fallidos en la ventana actual.
- Las 28 denegaciones históricas de `/v1/extract` proceden de un único grupo IP seudonimizado, que también tuvo requests correctas de perfil/cola. Hubo eventos `extract_ip_rate_limited` y `extract_user_rate_limited` para ese grupo; no aparecen eventos de rate limit general en 7 días. Los registros históricos no permiten asociar el grupo a una cuenta ni distinguir `FREE_YEARLY_LIMIT` de `PRO_FAIR_USE_LIMIT` en los 403. Los 429 de otros endpoints tampoco tienen un código de causa correlacionable. El cambio local añade esa evidencia para los siguientes rechazos una vez desplegado; no revela el motivo de los eventos pasados.

El probe público de `/ready` devuelve 200 mientras la consulta directa confirma que faltan columna e índice; por tanto, la versión desplegada no está ejecutando la comprobación estricta del checkout actual. El cliente envía `client_delivery_id` y la API consulta esa columna para deduplicar, así que los imports nuevos desde ShareInbox no pueden completar hasta aplicar 008. El código local de `/ready` valida ambos objetos y devuelve 503 si falta alguno.

La migración 008 solo añade una columna nullable y un índice parcial único; no modifica filas existentes. No se aplicó en producción durante esta revisión.

### APNs: código listo localmente; producción y dispositivo pendientes

El código local ya registra tokens y envía notificaciones por APNs al completar extracción nueva o traducción solicitada; cache hits no generan alerta. La migración 010 y los secretos `APNS_*` aún requieren rollout coordinado con el worker y el cliente. El idioma se toma de la preferencia de ReciApp, no del idioma global del dispositivo. Si APNs o worker no están listos, registro responde `push_enabled=false` y el cliente conserva fallback local. APNs sigue siendo best-effort; el Simulator no prueba token real, firma ni entrega. Producción no se pudo consultar el 2026-09-27 porque DNS no resolvió el host; las observaciones de servidor/deployment debajo son del 2026-09-26.

### Worker de extracción: cambio de infraestructura preparado, no desplegado

La configuración Compose inspeccionada tenía solo `recipe-backend`; la lectura de configuración efectiva dentro del API confirma `WORKER_ENABLED=false`, y la inspección remota no encontró contenedor `recipe-worker`. Si el proceso API se reinicia durante una extracción, FastAPI puede perder la tarea en memoria y la fila queda para recuperación posterior. Preparé `recipe-worker` en el commit local `1de66d1` de `codex/reciapp-durable-worker`, con el mismo image/env, acceso privado a Postgres y leases. El repo de infraestructura no tiene remoto Git configurado, así que el commit sigue local. La red `reciapp-internal` permite salida (`internal=false`). `/ready` comprueba la base y el esquema, no que el worker esté vivo; después del rollout también hay que verificar el contenedor y su log de arranque.

### Pro en producción: webhook recibe eventos; falta compra/restauración controlada

La integración de código está: `SubscriptionService.identify()` envía el UUID backend como `user_id`; `/v1/webhooks/superwall` exige firma Svix y actualiza `profiles.is_pro`; la app refresca `/v1/me` tras compra/restauración. Verifiqué sin leer secretos que el secreto del webhook está configurado. En `subscription_events` hay 7 eventos Superwall en los últimos 30 días: 7 procesados, 0 fallidos (3 eventos Pro-on, 2 Pro-off y 2 cambios de estado). Esto confirma recepción y procesamiento real del webhook. Sigue sin probarse una compra/restauración sandbox completa en un dispositivo junto con el `GET /v1/me` de esa misma cuenta.

### Pruebas y producción

- En esta revalidación, `xcodebuild` Debug para iOS Simulator sin firma completó con `ONLY_ACTIVE_ARCH=YES ARCHS=arm64` y produjo `ReciApp.app`, también tras el fix del progreso de cocción. El primer intento con ambas arquitecturas agotó espacio en `/tmp`; retiré esos DerivedData temporales y recuperé espacio. `swiftc -frontend -parse` sobre todos los `.swift` también terminó con código 0. Es evidencia de compilación, no ejecución de la app: no se probó en Simulator ni en iPhone, y no se produjo IPA firmado.
- Pruebas Swift del `ClientStateHarness`: 48 pasaron en esta revalidación.
- Suite completa servidor: 265 pasaron, 2 omitidas en la ejecución más reciente. Las 2 omisiones son contratos cruzados que también están cubiertos por harness. La ejecución local usa shim temporal para Sentry, por lo que telemetría Sentry no queda validada.
- Reejecuté en esta continuación `python3 -m pytest -q tests/test_reliability_contract.py`: 36 pasaron y 2 omitidas; incluye rutas iOS↔FastAPI, modelos de import/reintento, refresh, ShareInbox, locales y purga de datos. No pude usar el comando `pytest` directo porque no está instalado como ejecutable en PATH; el módulo de Python sí está disponible.
- Tras el arreglo de sincronización Pro, `xcodebuild` Debug genérico para iOS Simulator pasó con firma desactivada y generó `ReciApp.app`; warnings de linker permanecen. `ClientStateHarness`: 48 pasaron. Estos tests no simulan compra ni entrega APNs real.
- No hubo ejecución visual ni prueba firmada en dispositivo.
- Consulté el SQL real de readiness en un PostgreSQL temporal aislado: migración 008 correcta → `delivery_column=t`, `delivery_index=t`; índice no único y mal definido con el mismo nombre → `delivery_index=f`.
- El contenedor de producción tiene DSN Sentry configurado y entorno `production`; no pude consultar issues porque falta `SENTRY_AUTH_TOKEN` local. No se leyó ni compartió ningún token ni DSN.
- En la revalidación pública anterior, `/health` y `/ready` respondieron HTTP 200. En esta continuación no pude volver a consultar producción: DNS no resolvió `51-255-43-100.sslip.io`; por tanto, los 200 anteriores no son una comprobación de ahora. La respuesta previa de `/ready` tampoco identificaba build ni comprobaciones, así que no confirmaba por sí sola 008/009.
- Reintento de esta auditoría (2026-09-26): `curl` devolvió HTTP 000 por `Could not resolve host` tanto para `/health` como para `/ready`, y el navegador de investigación tampoco pudo abrirlos. Probé HTTPS fijando el hostname a la IP codificada por `sslip.io`; la conexión al puerto 443 también falló antes de obtener respuesta. Esto confirma que desde este entorno no puedo verificar la API ahora; no prueba por sí solo una caída global del servicio ni un fallo en los dispositivos de usuarios.
- Acceso SSH de solo lectura al VPS sí funcionó en esta continuación. El contenedor Coolify de la API está `healthy`, usa imagen etiquetada con el commit `36a7cc16f7c9b33e67aa9132908d25c18ee40857` (`main`) y Traefik responde `/health` y `/ready` con HTTP 200 desde el propio host. Consulté únicamente metadatos del esquema mediante el proceso de la API: `client_delivery_id=false`, índice único de 008 `false` y columnas de replay 009 `false`. El mismo proceso informa `WORKER_ENABLED=false`; no existe contenedor `recipe-worker`. Esto confirma que la API está arriba pero la integración de las ramas auditadas no está desplegada y el readiness actual no protege contra el esquema incompleto.
- Revalidación SSH de solo lectura, 2026-09-27: API `running/healthy` y `/ready` devuelve 200 desde dentro del contenedor, pero su configuración ni siquiera contiene los campos APNs (`APNS_ENABLED` false/ausente). PostgreSQL sigue sin columna ni índice 008, sin las tres columnas 009 y sin tablas/trigger de 010. `WORKER_ENABLED=false`, no hay contenedor worker ni heartbeat reciente. Por tanto, API saludable/readiness 200 no significa que la integración actual esté desplegada; imports idempotentes, replay de refresh y APNs siguen sin soporte en producción.
- Agregados de `api_request_logs` en las 24 h más recientes: `/v1/auth/refresh` 1×200, `/v1/me` 3×200, `/v1/me/jobs` 3×200 y `/v1/me/recipes` 1×200; no hay requests a `/v1/extract` ni jobs nuevos. También figura un `/v1/metadata` 404, ruta que no aparece en el cliente ni API actuales; origen desconocido. Sin solicitudes de extracción recientes no se puede concluir que imports funcionen ni atribuir un fallo activo a la app.
- No se probó Apple login ni una extracción real en dispositivo; tampoco la compra/restauración sandbox de extremo a extremo, aunque sí hay webhooks Superwall procesados en producción.
- Los cambios están publicados en las ramas remotas `codex/reciapp-server-integration` (`aa5d0db`, último HEAD) y `codex/reciapp-ios-integration` (`864a487`, último HEAD). El repo del backend incluye `docker-compose.worker.yml`; su YAML se parseó localmente, pero Docker CLI no está instalado, así que no se pudo validar con `docker compose config`. La integración aún no se ha desplegado ni publicado en App Store. El repo ops tiene además el rollout de worker en un commit local de `codex/reciapp-durable-worker`, pero no tiene remoto configurado.

### Historial anterior al rollout: migraciones de producción, 2026-09-27

Los puntos siguientes registran el estado intermedio antes de terminar el rollout; el estado vigente está en “Rollout de producción completado”.

- Antes del cambio generé un dump PostgreSQL adicional y comprobé gzip y cabecera del dump. Está en el almacén diario de backups del VPS.
- Apliqué 008 y 009 en orden. Luego 010 se detuvo porque faltaban `app_is_service()` y `app_user_id()`; la base tampoco tenía registro de migraciones, políticas RLS ni RLS activado en tablas existentes. `reciapp` además figura como superusuario y `BYPASSRLS`. No apliqué migration 002 en bloque ni rebajé ese rol: ambas acciones cambian el modelo de seguridad completo y requieren una migración/rollout separado con grants probados.
- Hice 010 repetible y autocontenida para esta recuperación: define esos dos helpers, tolera tablas/índices ya creados, reemplaza de forma idempotente las políticas del outbox y trigger. La reapliqué en una transacción; verificación SQL confirmó columna/índice 008, tres columnas 009, tablas y políticas 010, trigger, helpers y función de logout. API actual siguió `running/healthy`; `/ready` interno dio 200.
- Código de servidor aún no está desplegado: API corre imagen antigua `36a7cc16…`, no reconoce `APNS_ENABLED`, `WORKER_ENABLED=false` y no existe worker. No activar procesamiento durable ni push hasta desplegar el API actualizado y worker con misma versión. APNs sigue sin configuración en el contenedor actual.
- Publiqué el arreglo en `codex/reciapp-server-integration`, commit `72b843e`. Después, GitHub API no resolvió DNS y no pude consultar/mezclar el PR ni desplegar la rama. El API sigue en la imagen anterior hasta completar ese rollout.
- Ensayé la política RLS en una base temporal restaurada del respaldo anterior a las migraciones, sobre PostgreSQL 16. Apliqué 008–011 y probé con rol no-superusuario: el usuario vio solo su perfil/biblioteca/jobs propios y jobs compartidos; no vio jobs pendientes de otra cuenta ni eventos anonimizados; servicio vio el conjunto y `claim_next_extract_job` reclamó trabajo con FORCE RLS. Eliminé DB y rol temporal al terminar.
- Añadí `011_row_level_security_hardening.sql`, política-only: no reemplaza funciones de negocio, quita visibilidad global de jobs pendientes y evita que cuentas normales lean eventos de gasto anonimizados. Suite servidor: 266 passed, 2 skipped. Esta 011 está ensayada, todavía no aplicada a producción.
- Última consulta disponible de GitHub API, anterior al commit iOS `864a487`: ambos PR #1 estaban abiertos en borrador, mergeables, sin checks configurados. La API no respondió en la revalidación posterior, así que ese estado no está confirmado para los HEAD actuales. Las descripciones de PR tenían conteos antiguos (246 server, 46 harness); evidencia local actual: 265/2 server, 36/2 contratos, 48 harness y build Debug Simulator pasado.

### Rollout de producción completado — 2026-09-27

- Se publicó y desplegó `4e10837f64164271d43ad81f9023a53f3ad77df5` en la API. La imagen de la API y la del worker coinciden.
- Migración 011 aplicada después del backup verificado. RLS activo en 18 tablas. Se creó `reciapp_runtime` sin `SUPERUSER` ni `BYPASSRLS`, con grants de tablas, secuencias, funciones y privilegios por defecto; API y worker usan este rol. El proveedor impide quitar `SUPERUSER` al rol bootstrap `reciapp`, así que ese rol queda fuera de los servicios runtime.
- `WORKER_ENABLED=true`; el contenedor `reciapp-worker` está arriba y escribe heartbeat fresco. API `/health` y `/ready` devolvieron 200.
- El worker no ofrece HTTP; desactivé para él el healthcheck heredado de la imagen (que consultaba `/health`). Su señal de salud es el heartbeat que exige `/ready`.
- Smoke test desde la conexión real de la API: `current_user=reciapp_runtime`; contexto de usuario aleatorio vio cero perfiles y trabajos; contexto `service` vio el heartbeat del worker.
- APNs permanece apagado. Tras la autorización del usuario, probé la clave `.p8` en APNs con un token ficticio que no puede recibir una notificación. La firma local fue válida, pero Apple respondió `InvalidProviderToken`; retiré la clave del VPS y no hubo filas en el outbox. Hace falta una clave APNs válida y confirmar su Team ID. La entitlements de iOS está presente, pero la entrega no se verificó.
- No fue posible probar Apple login, Pro, importación o notificaciones en un iPhone: este host no tiene identidad de firma válida ni iPhone conectado. El simulador tampoco completó ejecución real en esta sesión.

## Orden de aceptación antes de la revalidación actual

1. Autorizar o rechazar la transferencia de la clave APNs; si se autoriza, validar la clave sin enviar una notificación a un usuario real y completar el registro/dispositivo sandbox.
2. Conseguir un iPhone y una identidad de firma válida; probar login Apple, Pro/restore, importación nueva/cacheada, biblioteca, traducción, reintento, borrado y notificación con Wi-Fi y red móvil.
3. Mantener pendiente la afirmación de “integración completa en dispositivo” hasta completar esos pasos; el estado de API/worker/RLS sí quedó verificado en producción.

## Revalidación en el host Codex — 2026-09-27

- La API de producción respondió `/health` 200 y `/ready` 200 (`ready`, `environment=production`, `maintenance=false`). El probe `/ready` ejecuta las comprobaciones de migraciones 008–010, el trigger de push, la función de logout y un heartbeat reciente de `recipe-worker`; el 200 actual demuestra que esos contratos pasan en la instancia consultada.
- Suite del servidor: 270 passed, 2 skipped. El entorno `.venv` local tiene un intérprete roto; la suite se ejecutó con Python 3.11 y los paquetes ya instalados, sin cambiar dependencias.
- iOS `codex/reciapp-ios-integration` (`864a487`) compila en Debug y Release para iOS Simulator; la app Debug instala y arranca en iPhone 17 Simulator. `ClientStateHarness`: 48 passed; `PricingExperimentTests`: 15 passed. Después se avanzó `main` por fast-forward hasta `864a487`. Esto no prueba firma ni entrega APNs real.
- La clave `.p8` local se pudo cargar como clave privada válida. Una petición al endpoint de producción APNs, firmada con el Key ID y Team ID confirmados y topic `com.membri.reciapp`, devolvió `BadDeviceToken` para un token de prueba inválido. Apple aceptó la autenticación; no se notificó ningún dispositivo.
- No se pudo leer ni actualizar Coolify desde este host: el acceso de Computer Use a Vivaldi fue rechazado y SSH al VPS rechazó la clave disponible. Por ello no se confirma el `APNS_TEAM_ID` actual del API y worker; configúralo con el Team ID confirmado en ambos servicios antes de considerar listo el envío remoto.
- Quedan pendientes: confirmar la variable APNs y reiniciar API/worker si cambia; revisar incidencias recientes de Sentry (no hay `SENTRY_AUTH_TOKEN` disponible en este host); registrar una instalación Release real; probar entrega en un iPhone; distribuir la app. `main` recibió un fast-forward; no se hizo TestFlight ni publicación App Store.

## Revalidación en el host Codex — 2026-10-01

### Restauración de Pro: código local verificado; despliegue pendiente

- Antes, la app llamaba a Superwall para restaurar y después solo consultaba `/v1/me`. No enviaba la transacción restaurada al servidor; si faltaba el webhook, el estado podía quedarse indefinidamente en “Pro syncing”.
- iOS lee transacciones activas verificadas de `Transaction.currentEntitlements`, envía sus JWS a `POST /v1/me/subscription/restore` y muestra progreso/errores. También conserva restaurables los dos SKU anteriores (`reciapp_wk`, `reciapp_an_3trial`) y comprueba primero las transacciones locales; solo invoca `AppStore.sync()` si StoreKit no devuelve ninguna. El servidor acepta y verifica los 11 SKU actuales e históricos.
- Se reprodujo una causa concreta del botón “Restore purchases / refresh access” sin respuesta: el aviso superior tenía un `DragGesture` exclusivo que podía consumir el toque. Inicio y Ajustes usan ahora `simultaneousGesture`; el arreglo está solo en el árbol local de iOS y requiere nueva compilación/distribución.
- FastAPI verifica firma JWS, bundle ID, entorno `Production`, lista de productos permitidos, expiración, revocación y `appAccountToken` contra el usuario autenticado. Reutiliza columnas existentes; **no requiere migración**.
- El endpoint requiere `APPLE_ROOT_CA_PEM`. `.env.example` lo deja vacío y el endpoint responde 503 si no está configurado. Comprobación de solo lectura del 2026-10-02: API activa aún no declara `/v1/me/subscription/restore` y tampoco tiene `APPLE_ROOT_CA_PEM`; el botón de la app no puede completar la restauración en producción. Tras desplegar el servidor con la ruta, Coolify debe incluir el certificado raíz Apple en PEM; no es la clave privada APNs `.p8`.
- Verificación local: servidor `275 passed, 2 skipped`; compilación iOS Simulator `BUILD SUCCEEDED`; `git diff --check` y catálogo JSON válidos. No se probó la compra/restauración en iPhone.
- Los cambios están sin commit en `codex/reciapp-server-integration` y `codex/reciapp-ios-integration`. Coolify/App Store todavía no pueden contenerlos por el flujo normal de ramas.

### Estado actual de producción y límites

- El 2026-10-01 `curl` devolvió HTTP 000 y `Could not resolve host: 51-255-43-100.sslip.io`. La prueba TLS fijando `51.255.43.100` tampoco conectó. No demuestra caída global; desde este host no hay prueba actual de `/health`, `/ready`, esquema, worker, APNs ni versión desplegada.
- El usuario informó que configuró APNs en Coolify. La lectura efectiva de `APNS_ENABLED`, `APNS_TEAM_ID` y `APNS_ENVIRONMENT` sigue sin confirmarse.
- El último dato documentado de Sentry es DSN configurado en producción y entorno `production`; no se refrescó. `SENTRY_AUTH_TOKEN`, `SENTRY_ORG` y `SENTRY_PROJECT` faltan en este host, así que no se consultaron incidencias actuales.
- La biblioteca de recetas se sirve desde `/v1/me/recipes`; extracción completada adjunta receta al usuario en servidor. Tras reinstalar, recetas vuelven al iniciar sesión en la misma cuenta. El snapshot de estado personal también sincroniza carpetas, favoritos, etiquetas, layouts, lista de compra, preferencias y progreso de cocina; ambos requieren misma cuenta ReciApp. La captura “Uncategorized · 0 recipes” no permite distinguir cuenta distinta de biblioteca realmente vacía; requiere comprobar el UUID/cuenta y la respuesta autenticada de biblioteca, sin exponer tokens.
- Revisión de rutas iOS ↔ FastAPI: los 16 métodos/rutas del cliente coinciden con las rutas declaradas en `app/main.py` (auth, perfil, restauración, push, borrado, importación, trabajos, biblioteca y estado cloud). `tests/test_ios_api_contract.py` detecta rutas huérfanas y faltantes. `GET/PUT /v1/me/library-state` usa revisión CAS; servidor requiere migraciones 012/013 antes de rollout.
- Catálogo de notificaciones: regeneré `app/apns_localizations.json` a `/private/tmp` desde `ReciApp/Localizable.xcstrings`; comparación byte a byte coincide en los 50 idiomas configurados.
- Suite del servidor tras añadir el contrato de rutas: `276 passed, 2 skipped`.
- Implementación local de persistencia de estado: migraciones 012/013, tabla JSONB con FORCE RLS y política solo de servicio, límite de snapshot 256 KiB, validación de `schema_version`, control CAS, historial de tres versiones y `/ready` fail-closed para esquema/política incompletos. PostgreSQL 14 temporal aplicó 001–013 y verificó CAS/RLS; suite completa actual `288 passed, 2 skipped`. Producción requiere 012/013 antes de desplegar este backend.
- Sincronización iOS de estado personal completada después; ver “Continuación — resolución segura de estado local/cloud”. Conserva copia local y ofrece combinar/elegir copia en conflicto; poda referencias a recetas borradas. Build, despliegue y aceptación física siguen pendientes.
- Pendiente aceptación real: aplicar 012/013 en producción; desplegar API con `APPLE_ROOT_CA_PEM` y confirmar worker/APNs en Coolify; compilar y distribuir app; probar reinstalación, restauración Pro, biblioteca/estado cloud y APNs en iPhone con misma cuenta ReciApp y Apple ID; comprobar `/v1/me.is_pro=true`. Sentry iOS DSN no tiene inyección de build demostrada. Hasta eso, integración de producción/dispositivo sigue sin verificar.

### Continuación de auditoría — 2026-10-01

- iOS restauración: el flujo manual consulta primero `Transaction.currentEntitlements` y solo llama `AppStore.sync()` si no hay transacciones; filtra verificadas/activas, las envía al endpoint de restore y consulta perfil si Apple no devuelve transacciones. Si backend confirma Pro pero perfil aún muestra estado antiguo, conserva acceso en memoria. Inicio y Ajustes usan botones con gesto simultáneo para que el arrastre de la notificación no consuma el toque. El soporte de los dos SKU antiguos se añadió después, en “Contrato de restauración StoreKit”.
- No hay prueba de compilación/runtime para este cambio: `xcodebuild` no resolvió dependencias porque el host no pudo resolver `github.com`; Simulator tampoco está disponible. No afirmar que está instalado en el iPhone hasta crear/distribuir nuevo build.
- Revisé estáticamente las rutas iOS y FastAPI: todas las rutas del cliente tienen endpoint servidor equivalente, incluidos `/v1/me/subscription/restore` y `/v1/me/library-state`.
- Hallazgo bloqueante en iOS: `syncLibraryState()` invoca `currentLibraryStateSnapshot`, `storedLibraryStateBaseline`, `persistLibraryStateSync`, `applyLibraryState`, `clearLibraryStateSyncError` y `showLibraryStateSyncFailure`, pero esas funciones no existen en el checkout. Tampoco hay llamada automática al sincronizador al activar cuenta ni observadores completos de cambios locales. El código de sincronización actual, por tanto, no está listo para compilar ni respaldar/restaurar datos.
- No añadí escritura remota sobre estado local. El revisor automático rechazó reemplazar carpetas, favoritos, etiquetas, lista, preferencias y progreso locales con el snapshot servidor por riesgo de pérdida irreversible. Está pendiente decidir el flujo de conflicto; elección recomendada: conservar ambos y pedir elección/combinación explícita.
- Suite servidor revalidada en este host con intérprete 3.11 y paquetes del entorno existente: `283 passed, 2 skipped`. El ejecutable del `.venv` apunta a otro checkout inexistente; ejecutar con `PYTHONPATH=.venv/lib/python3.11/site-packages python3.11 -m pytest -q`.
- Producción no verificable desde este host: `/health` y `/ready` dieron HTTP `000`; el dominio no resolvió. Sentry iOS tiene placeholder `$(SENTRY_DSN)` en Info.plist y el repositorio documenta que sin inyectar `SENTRY_DSN` queda desactivado; no se encontró build setting/CI en este checkout que demuestre la inyección. No confirma si Xcode Cloud/App Store Connect añade esa variable externamente.

### Continuación — resolución segura de estado local/cloud

- iOS sincroniza el snapshot por cuenta tras una respuesta correcta de biblioteca y ante cambios de carpetas, lista de compra, preferencias y progreso de cocina. Si el dispositivo está vacío, restaura la copia cloud; si ambas copias difieren, ofrece combinar, usar cloud o mantener este dispositivo. Las escrituras usan CAS por revisión.
- Antes de aplicar una elección, la copia local se archiva en dispositivo. Servidor conserva tres revisiones anteriores en JSONB; Ajustes permite restaurar copias locales o cloud. Restaurar desde cloud primero guarda estado actual en servidor y luego aplica la versión elegida. Un conflicto concurrente reintenta sin reemplazar estado local.
- Añadida migración aditiva 013 para historia de snapshots; `/ready` exige columna JSONB y constraint de array. Aplicar 012 y 013 antes de desplegar este backend. No se aplicaron migraciones en producción.
- Suite server actual: `287 passed, 2 skipped`; `compileall` y `git diff --check` pasan. El primer intento de PostgreSQL temporal falló por `shmget` bajo sandbox; la revalidación posterior sí aplicó y ejercitó SQL en PostgreSQL 14, detallada abajo.
- iOS `swiftc -frontend -parse` pasa para modelos, stores, view model y vistas modificadas. Build Xcode sigue sin verificarse porque SPM no pudo resolver GitHub; StoreKit real, ejecución visual, instalación y reinstalación aún requieren build/dispositivo.

### Revalidación PostgreSQL temporal — 2026-10-01 15:18 CEST

- Repetí las migraciones `001`–`013` en una instancia PostgreSQL 14 aislada, local y efímera; todas aplicaron sin error. Consultas reales confirmaron las cinco columnas de `user_library_state`, RLS y FORCE RLS activados, y el constraint de historial validado.
- Ejercité cinco actualizaciones CAS en esa base: revisión final `6`, historial limitado a tres snapshots anteriores (`3,4,5`). La instancia y sus datos temporales se eliminaron al terminar.
- Con rol temporal `reciapp_runtime` sin bypass/superuser, confirmé que contexto `user` ve cero filas y contexto `service` puede leer y actualizar el snapshot. SQL fallaba explícitamente si cualquiera de esas aserciones no se cumplía; ambas pasaron.
- Esto valida sintaxis y comportamiento SQL de migraciones en PostgreSQL 14, no la base de producción ni las funciones API conectadas a Coolify. Migraciones `012` y `013` siguen pendientes de aplicación en producción.
- Intenté compilar iOS en destino genérico con DerivedData y SwiftPM aislados en `/private/tmp`. Xcode descargó artefactos y llegó a ocupar 3,8 GB; quedaban 1,2 GB libres, así que interrumpí la compilación para proteger el disco y borré únicamente ese directorio temporal. Ahora quedan 3,6 GB libres. La compilación sigue sin verificar; no repetir hasta disponer de espacio suficiente.

### Contrato de restauración StoreKit — 2026-10-01

- La allowlist servidor y catálogo iOS incluyen nueve SKU actuales y dos SKU históricos del catálogo StoreKit. `SubscriptionRestoreRequest` acepta hasta once transacciones; cada JWS sigue limitado a 16 KiB y el cuerpo queda bajo el límite HTTP de 256 KiB.
- El contrato cruzado deriva productos actuales e históricos de iOS y exige igualdad con allowlist servidor; pruebas aceptan once y rechazan doce. Prueba adicional restaura un SKU histórico.
- Pruebas objetivo: `8 passed`; suite servidor actual: `288 passed, 2 skipped`. Parse Swift y `git diff --check` pasan. Build Xcode actual no verificado: resolución SPM falla porque este host no resuelve `github.com`.

### Límite de tamaño del backup cloud — 2026-10-01

- El cliente comprueba el tamaño serializado de cada escritura cloud contra el límite HTTP de 256 KiB antes de enviarla. Los errores permanentes 413/422 dejan de ofrecer un reintento inútil y explican que los datos siguen en el dispositivo. `swiftc -frontend -parse` y pruebas server de estado/rutas (`9 passed`) pasan; build iOS completo pendiente.

### Verificación de fuentes actuales — 2026-10-01

- Parseé todos los archivos Swift de `ReciApp` y `ReciAppShare`; también pasaron el catálogo `.xcstrings` como JSON, los dos `Info.plist`, `project.pbxproj` y `git diff --check`.
- Suite servidor actual: `287 passed, 2 skipped`; `compileall` pasa. La compilación iOS completa sigue pendiente: el intento anterior consumió casi todo el espacio libre de `/private/tmp` al descargar artefactos SPM y se detuvo de forma controlada.

### Referencias a recetas borradas al restaurar estado — 2026-10-01

- `applyLibraryState` ahora reconcilia asignaciones, favoritos y etiquetas con los IDs presentes en la biblioteca recién cargada. Si elimina referencias obsoletas o normaliza valores, persiste la copia local y programa una actualización cloud CAS, también tras restauraciones o combinaciones explícitas.
- Todos los fuentes Swift pasan `swiftc -frontend -parse` y `git diff --check`; ejecución/build iOS sigue pendiente.

### Validación del harness iOS — 2026-10-01

- `ClientStateHarness` fallaba al compilar: los modelos de snapshot cloud no estaban incluidos en el harness. Añadí los modelos equivalentes y pruebas de merge con cambios independientes, eliminaciones locales, lista de compra, preferencias/progreso de cocina y decodificación de snapshots previos.
- `ClientStateHarness`: 50 tests pasan. `PricingExperimentTests`: 15 pasan. Ambos usan paquetes Swift locales y no requieren resolver dependencias externas.
- Intento de build `ReciApp` Debug Simulator falla antes de compilar fuentes con `Could not resolve package dependencies`; el host no resuelve `github.com`. Se retiró únicamente el DerivedData temporal de `/private/tmp`; quedan 3,1 GB libres. Build completo y prueba de dispositivo siguen pendientes.
- Producción vuelve a ser inaccesible desde este host: el dominio API no resuelve y `/health`/`/ready` no entregan respuesta. No se desplegó ni se tocó Coolify.

### Reintento de restauración tras reinstalar — 2026-10-01

- Revisión de la pantalla reportada: el aviso de Pro ofrecía restaurar solo como acción dentro de un banner superior. El gesto vertical del banner podía competir con el toque, y Ajustes no ofrecía restauración cuando el estado local ya marcaba Pro.
- Quité el gesto del banner y amplié el botón a un área táctil mínima de 44 pt. El botón muestra progreso mientras StoreKit y el servidor reconcilian la compra. Añadí en Ajustes una acción persistente para restaurar/actualizar Pro tanto con estado local Free como Pro.
- Build iOS Debug para Simulator: correcto, 0 errores; queda una advertencia del linker `address=0x33B6F3 points before section(29) start and the target atom is ambiguous`. `git diff --check` y `plutil -lint ReciApp.xcodeproj/project.pbxproj` pasan.
- Los cambios de UI aún no están distribuidos; no se verificó el toque con una compra real ni la restauración contra la instancia de producción. El servidor ya tiene la ruta `/v1/me/subscription/restore`; no requiere migración nueva para este arreglo.

### Reinstalación: reconciliar compra y volver a cargar recetas — 2026-10-01

- Hallazgo adicional: `refreshAll()` podía reemplazar el error de descarga de recetas por el aviso de Pro pendiente. La acción de restaurar Pro limpiaba el aviso, pero no volvía a solicitar `/v1/me/recipes`; tras reinstalar podía quedar la biblioteca vacía aunque el reintento de compra terminara bien.
- Corregí prioridad de recuperación: cuando fallan recetas, se conserva el mensaje y botón **Refresh**; el estado local Pro sigue disponible y Ajustes conserva **Restore purchases / refresh access**. Si restauración Pro termina, la app vuelve a pedir perfil y recetas; el resultado nuevo decide si limpia o conserva el estado de recuperación.
- Añadí prueba de prioridad para los casos «fallan recetas + Pro pendiente», «solo Pro pendiente» y «sin error». `ClientStateHarness`: 51 passed; `PricingExperimentTests`: 15 passed.
- Servidor: `286 passed, 2 skipped`; ambos skips indican cobertura sustituida por `ClientStateHarness`. `compileall` pasa. iOS Debug Simulator y Release iOS compilan con cero errores. Debug conserva una advertencia del linker en ReciApp; Release no muestra advertencias.
- Prueba remota actual: `/health` y `/ready` devuelven HTTP 000; DNS no resuelve `51-255-43-100.sslip.io` y conexión TLS fijando el host también falla desde este equipo. Esto confirma falta de acceso desde este host, no caída global. Base de datos configurada en el repo es PostgreSQL autohospedado en el VPS, no Supabase. El proyecto Supabase llamado ReciApp está `INACTIVE`, pero no es el almacén configurado para este backend.
- Contratos locales: las 16 rutas de API/Auth usadas por iOS están declaradas en FastAPI; los 11 productos StoreKit coinciden con la allowlist servidor. El catálogo APNs regenerado desde iOS coincide exactamente en los 50 idiomas soportados.
- Sentry iOS no quedó activado en los productos Release inspeccionados: `ReciApp.app` y `ReciAppShare.appex` conservan `$(SENTRY_DSN)`, que el código ignora. El repo iOS sí incluye `SENTRY_SETUP.md` con inyección manual durante Archive; no hay automatización de build/CI que pruebe que se ejecutó. El DSN del servidor es configuración aparte; definirlo en Coolify no activa Sentry iOS. Verificar los dos Info.plist archivados y un evento recibido.
- Aún sin prueba real: que la base del VPS tenga 012/013, variables activas de Coolify/APNs/`APPLE_ROOT_CA_PEM`, compilación instalada, la misma cuenta Apple/ReciApp y restauración/recarga de recetas en iPhone. No se desplegó nada.
- Los refrescos superpuestos ahora llevan contador de generación: una respuesta antigua ya no puede sobrescribir biblioteca/perfil más nuevos ni bajar antes de tiempo el indicador de carga. La compilación iOS verifica integración.

### Aviso de restauración persistente — 2026-10-01

- Hallazgo tras el reporte del usuario: el aviso de recuperación se autocerraba a los dos segundos. Restaurar puede requerir interacción con Apple o una respuesta lenta del servidor, por lo que el botón/progreso desaparecía y parecía no responder.
- El aviso ahora permanece visible cuando tiene una acción de recuperación o hay restauración en curso. Se cierra al completar la operación o mediante el botón Cerrar; el gesto del banner sigue siendo simultáneo al toque del botón.
- `git diff --check` y parseo de todos los fuentes Swift pasan. Suite servidor: `286 passed, 2 skipped`. Build Xcode no pudo resolverse aquí: SwiftPM/Xcode intentaron escribir cachés bajo `/Users/andrescasillas/Library` fuera del área permitida; el servicio Simulator reportó conexión inválida. Requiere build firmado, instalación y prueba de restauración en iPhone; este cambio aún no está distribuido.

### Runbook de despliegue: migraciones y worker — 2026-10-01

- La guía de operaciones del repo `Server` seguía indicando aplicar solo hasta la migración 010, aunque el API actual necesita 011-013; también describía el recurso API de Coolify sin dejar claro que no arranca el worker. Actualicé `Server/INFRASTRUCTURE.md`: secuencia autoritativa hasta 013, estado productivo como no verificado y requisitos del worker para procesamiento durable/APNs.
- Contrato comprobado en código: con `WORKER_ENABLED=false`, `PUT /v1/me/push-device` informa `push_enabled=false`; la app debe usar notificaciones locales. Para notificación remota, ambos servicios necesitan worker/heartbeat y credenciales APNs; `/ready` valida heartbeat cuando el modo durable está activo.
- Producción no se pudo comprobar desde este equipo: DNS de `51-255-43-100.sslip.io` sigue sin resolver. Las pruebas locales server pasan (`286 passed, 2 skipped`); parseo Swift y checks de plist/JSON pasan. No hubo despliegue ni prueba física.

### Contratos API y APNs — 2026-10-01

- Revisión estática del cliente: las 16 rutas HTTP/Auth que usa `APIClient`/`AuthService` existen en FastAPI; codificación/decodificación snake_case corresponde a los modelos Pydantic. Catálogo StoreKit actual e histórico coincide con la allowlist de restore.
- Targets iOS declaran `aps-environment=development` en Debug y `production` en Release; ambos usan bundle identifier con dominio APNs esperado. Esto prueba configuración fuente, no entitlement dentro de una app firmada ni recepción real de APNs.
- Tests relevantes de contrato, confiabilidad, APNs y readiness: `61 passed, 2 skipped`. Quedan sin demostrar DB de producción, servicios Coolify, restauración StoreKit y push en dispositivo.

### Ciclo de sesión y borrado de cuenta — 2026-10-01

- Revisé Apple login, refresh rotativo, logout, sesión persistida en Keychain y borrado de cuenta frente a las rutas FastAPI. Respuesta de refresh omite usuario de forma intencional; el cliente conserva el usuario de sesión anterior. Borrado limpia recetas, push, acceso a jobs y revoca refresh tokens; conserva historial de cuota para impedir reinicios de límite.
- Tests de Apple auth, refresh/replay, borrado y errores Sentry: `31 passed`. No apareció desajuste de contrato en esta revisión. Aceptación real Apple/device sigue pendiente.

### Flujo de extracción y recuperación — 2026-10-01

- Revisé iOS → `POST /v1/extract` → persistencia del job → `GET /v1/jobs/{id}` → `/v1/me/jobs`, traducción enlazada por otro `job_id`, deduplicación Share Sheet y reanudación desde disco. UUID de entrega se conserva en ShareInbox y el servidor rechaza reutilización con URL/idioma distinto.
- Casos server de extracción, cuota/admisión, cola serial, fallos y traducción: `118 passed, 2 skipped`. No se encontró discrepancia estática en estados o campos Swift/Pydantic. El parseo/build del cliente y los casos de red/dispositivo no quedan demostrados por esos tests.

### Códigos de error y reintentos — 2026-10-01

- Contrasté errores FastAPI con `APIErrorDetailParser`/`APIErrorBehavior`: códigos de cuota, fair use, gasto, conflicto cloud, auth, validación, rate limit y fallos temporales tienen acciones cliente. Las respuestas 429 respetan `Retry-After`; reintentos se limitan a operaciones seguras y el POST de extracción no se repite automáticamente.
- `ClientStateHarness` contiene casos para 401/403/404/413/422/429/5xx, estados de job y formatos string/object/list. No repetí el harness en esta sesión porque su compilación previa quedó bloqueada por permisos de caché SwiftPM; el servidor sigue con `118 passed, 2 skipped` en la matriz de extracción.

### Sonda de readiness del despliegue — 2026-10-01

- Hallazgo: Dockerfile y Compose usaban `/health` como health check, que solo prueba liveness; Coolify podía aceptar un release aunque faltara PostgreSQL/migraciones o el heartbeat obligatorio del worker. Cambié Dockerfile, Compose y la guía de Coolify a `/ready`, que devuelve 503 al faltar schema requerido o worker cuando `WORKER_ENABLED=true`.
- Añadí prueba para mantener Dockerfile ligado a readiness. `tests/test_readiness_schema.py`: `10 passed`; suite server completa: `287 passed, 2 skipped`. Compose parsea como YAML y health check queda apuntando a `/ready`; `git diff --check` pasa.
- Reprobe desde este equipo: incluso fijando el nombre de API a su IP pública para saltar DNS, TCP/443 no conecta. Eso acota el fallo a conectividad/ruta desde este host, no demuestra caída global. Producción sigue sin verificarse.

### Precisión sobre la captura de Inicio — 2026-10-01

- Reinspección de la vista exacta: la captura usa `HomeView.appErrorToast`. Ese aviso no tiene gesto de arrastre ni temporizador de cierre; ambos existen en el aviso independiente de `ProfileView`. Por tanto, el auto-cierre/gesto no demuestra la causa del toque fallido de la captura; se corrige esa atribución de las notas anteriores.
- Inicio sí llama `performImportRecoveryAction()` y su acción `.restoreAndRefresh` inicia `restoreProAccess()`; el botón tiene objetivo mínimo de 44 pt y cambia a spinner/texto de restauración. El build nuevo y la prueba del toque en iPhone no están disponibles, así que no hay causa raíz confirmada para el toque reportado.

### Revalidación tras reanudar — 2026-10-02

- Suite server completa: `287 passed, 2 skipped`; `compileall` pasa usando caché en `/private/tmp`. Swift de app/extensión parsea; ambos plist y `project.pbxproj` validan; strings catalog es JSON válido; los tres repos pasan `git diff --check`.
- API producción sigue sin resolver desde este equipo. `/health` y `/ready` dan HTTP 000; conexión fijando la IP también falla en TCP/443. Esto no establece disponibilidad desde otras redes.
- Build Release iOS intentado con DerivedData/SPM bajo `/private/tmp`: falla al resolver paquetes porque `github.com` no resuelve aquí; el log muestra que PostHog, Sentry y Superwall no se pudieron clonar. CoreSimulatorService sigue inválido. No hay app firmada/instalada para prueba física.

### Reconciliación de Pro tras reinstalar — 2026-10-02

- Al revisar el toque de “Restore purchases / refresh access”, confirmé que Inicio y Ajustes llaman al flujo StoreKit → endpoint autenticado `POST /v1/me/subscription/restore`; el control ya tiene objetivo mínimo de 44 pt, estado de carga y mantiene el aviso. El código visible no demuestra por sí solo qué pasó en el iPhone del usuario.
- Hallazgo de servidor: una transacción vigente restaurada se aplicaba con su `purchaseDate` original. Si el perfil contenía una notificación posterior desactualizada, el control anti-eventos-fuera-de-orden descartaba la restauración válida. La restauración ahora se registra con la hora de reconciliación actual, conservando expiración y revocación verificadas en el JWS de Apple.
- Añadí una aserción para impedir volver a ordenar el restore por fecha de compra. Prueba enfocada: `10 passed`; suite completa con el intérprete del proyecto `.venv/bin/python`: `287 passed, 2 skipped`. El `python3` del sistema es 3.9 y no sirve para esta suite.
- No se pudo probar el botón en el dispositivo ni producción: API inaccesible desde este equipo y build iOS bloqueado por DNS de GitHub/SwiftPM. El repositorio `ReciApp-iOS` está fuera de las raíces con permiso de escritura de esta sesión; por tanto, aquí solo pude corregir y probar el servidor. Instalar/distribuir un build cliente sigue pendiente.

### Verificación iOS y observabilidad — 2026-10-02

- Resolví dependencias SwiftPM fijadas en caché temporal y compilé `ReciApp` Release para `generic/platform=iOS`, sin firma: `xcodebuild` terminó correctamente, sin errores ni avisos. `ClientStateHarness`: `51` pruebas pasan.
- Inspeccioné `SentryDSN` del `.app` generado: clave presente, valor vacío. Por eso Sentry iOS no está activo en este build; el SDK/código existen, pero falta inyectar DSN en configuración de Archive/TestFlight y verificar evento recibido. La variable `SENTRY_DSN` de Coolify afecta al backend, no al bundle iOS.
- El backend local inicializa Sentry solo si `SENTRY_DSN` está configurado; configuración de este checkout está vacía. No equivale a estado de Coolify.
- Sonda pública de solo lectura a `/health` y `/ready` sigue dando HTTP `000` desde este equipo. `CoreSimulatorService` sigue inválido; no hay dispositivo/simulador para probar el toque, StoreKit real, APNs ni captura de logs.
- El build Release corrige el estado de verificación anterior: el bloqueo de compilación SwiftPM quedó resuelto usando dependencias ya disponibles. El build se hizo sobre el checkout local actual; no prueba que ese binario se haya subido ni instalado.

### Compatibilidad del primer backup de biblioteca — 2026-10-02

- El primer `GET /v1/me/library-state` devolvía snapshot `{schema_version: 1}`. Swift sintetiza `Decodable` como campos obligatorios incluso si arrays/diccionarios tienen valor inicial; comprobé que faltas de `shopping_list` provocan `keyNotFound`. El cliente fallaba antes de crear su primer backup.
- Servidor ahora devuelve forma vacía completa. También completa campos opcionales de snapshots parciales al leer, escribir y devolver historial; valida el tipo de colecciones y conserva rechazo de versiones desconocidas/tamaño excesivo. Migración 012 usa el mismo snapshot completo como default.
- La forma vacía ahora se copia profundo por respuesta; una mutación accidental de una respuesta no altera el valor compartido del proceso.
- Prueba enfocada `8 passed` en `test_library_state.py`; suite server `288 passed, 2 skipped`. Verifiqué con Swift `JSONDecoder.convertFromSnakeCase` que el snapshot vacío nuevo decodifica.

### Estado remoto de notificaciones en la app — 2026-10-02

- Hallazgo: Inicio enseñaba el permiso de iOS como si las notificaciones de finalización remotas funcionaran, aunque el API respondiera `push_enabled=false`. Además, un registro APNs previo podía hacer que el fallback local se saltara después de un fallo de registro.
- ReciApp ahora muestra un aviso localizado cuando el permiso está concedido pero el servidor no puede enviar notificaciones. Si APNs o el registro API falla, borra el indicador remoto persistido para que vuelva a funcionar el fallback local.
- Verificación: Release iOS `generic/platform=iOS`, sin firma, compila con cero errores/avisos; `ClientStateHarness` pasa 51 pruebas; catálogo de localizaciones es JSON válido; `git diff --check` pasa.
- Límite: no se probó el toque ni la entrega en iPhone/TestFlight. El host no puede acceder al API de producción y no hay estado verificado del worker de Coolify; las notificaciones remotas siguen dependiendo del worker/APNs activos en producción.

### Aviso de disponibilidad remota y locales — 2026-10-02

- La prueba de catálogo encontró un nuevo aviso de APNs remoto con cobertura no inglesa de 46 idiomas; las cuatro variantes inglesas usan el texto fuente `en`, igual que la convención existente para avisos. El validador server no incluía este aviso en su allowlist de traducciones parciales y falló aunque el catálogo estuviera bien.
- Actualicé el validador para reconocer el aviso como notificación localizada parcial. Suite completa: `288 passed, 2 skipped`.
- Sentry app/extensión requiere `SENTRY_DSN` inyectado al Archive. No hay workflow CI, Fastlane ni script de Archive en el repo; el procedimiento está documentado manualmente en `SENTRY_SETUP.md`. El DSN vacío en el bundle local confirma Sentry inactivo en ese artefacto, no el estado de cualquier build distribuido.
- Build adicional con DSN ficticio (sin credenciales reales): Release iOS compiló sin avisos y `SentryDSN`/`SentryEnvironment=production` quedaron inyectados tanto en `ReciApp.app` como en `ReciAppShare.appex`. Esto verifica el mecanismo de build; no envió evento ni verifica configuración de Sentry/Coolify.
- `docker-compose.worker.yml` parsea como YAML y declara el comando `python -m app.worker`; `/ready` y worker siguen sin verificación live. La prueba local de `/ready` no pudo resolver el dominio de producción (`curl`: `Could not resolve host`).

### Borrado de cuenta y backup cloud — 2026-10-02

- Hallazgo: el cierre de cuenta anonimizaba perfil y datos asociados, pero dejaba `user_library_state`, porque `profiles` se conserva para reactivar la misma identidad Apple y la FK solo borraba snapshots ante DELETE físico. Eso podía recuperar carpetas, lista y progreso después de borrar la cuenta.
- Corregí la purga: cierra/bloquea el perfil primero y elimina el backup cloud dentro de la misma transacción. Los INSERT/UPDATE del snapshot bloquean también la fila del perfil y rechazan perfiles cerrados; una sincronización concurrente no puede recrear estado tras el borrado.
- La purga ahora establece contexto DB `service`: FORCE RLS solo permitía borrar esa tabla con ese actor. Añadí prueba para conservar este requisito.
- Prueba real local: apliqué migraciones 001–013 en PostgreSQL 14 temporal con FORCE RLS y rol `reciapp_runtime` sin superuser/BYPASSRLS; comprobé borrado del snapshot y rechazo de guardado tras cierre. Otro ensayo verificó que el guardado concurrente espera el lock y no recrea snapshot después del commit. Clústeres temporales eliminados.
- Suite server: `289 passed, 2 skipped`; `git diff --check` pasa. No modifica estado de migraciones productivas ni reemplaza aceptación en API/iPhone.

### Contratos JSON cliente-servidor — 2026-10-02

- Revisión adicional de requests/responses Swift y Pydantic: los campos camelCase/snake_case, campos opcionales y tipos de recetas, cuota, jobs, auth, compra, push y backup cloud no mostraron desajustes estáticos. `APIClient` y `AuthService` usan conversión snake_case recursiva.
- Detecté un hueco en `history` del backup: se exponía como `dict` sin validar, aunque iOS decodifica cada entrada como `revision`, `snapshot` y `updated_at`. Ahora el response model es tipado y descarta únicamente entradas de historial corruptas, manteniendo legible el snapshot actual.
- Reforcé `test_ios_api_contract.py`: compara las 16 rutas iOS con `APIRoute` real y exige `response_model` tipado para cada una. Tests de biblioteca: `9 passed`; suite server: `290 passed, 2 skipped`.
- Esto valida contrato fuente/modelos; no reemplaza llamada autenticada contra Coolify ni decodificación de respuestas de producción, inaccesible desde este host.

### Revalidación de producción y seguridad de recibos — 2026-10-02

- Acceso SSH de solo lectura volvió a funcionar. La API activa está `healthy`, pero usa commit `47b83a53efed`; no declara `/v1/me/subscription/restore` y carece de `APPLE_ROOT_CA_PEM`. El endpoint necesario no existe en esa imagen. Sin ambos cambios, restaurar compra falla.
- API tiene `WORKER_ENABLED=false` y `APNS_ENABLED=true`; el worker está activo con `WORKER_ENABLED=true`/`APNS_ENABLED=true`, heartbeat reciente (~18 s) e imagen distinta. API dice `push_enabled=false`, usa tareas en memoria y no el camino durable compartido. Deben desplegar misma imagen actualizada en ambos y activar worker en API.
- APNs team/key/topic están presentes y el topic coincide con bundle tanto en API como worker; root Apple no. API activa contiene rutas de recetas y registro push, pero no de restore ni backup cloud. El host que compila la app sí coincide con el router actual; no encontré desajuste de dominio/ruta.
- La API conecta como rol PostgreSQL superusuario con `BYPASSRLS`; la protección RLS no limita esas consultas. Existe `reciapp_runtime`, login sin superuser/BYPASSRLS; verifiqué permisos CRUD en las 20 tablas, funciones requeridas y secuencias presentes. API y worker deben usar el `DATABASE_URL` de ese rol.
- Producción aún carece de tablas 012/013. No desplegar sincronización cloud antes de aplicarlas con usuario administrador. No se escribió en producción.
- Hallazgo crítico en JWS: firmas `ES256` de Apple llevan `R‖S` de 64 bytes; `cryptography` espera DER. El servidor pasaba bytes JWS directamente, rechazando transacciones Apple válidas. Añadí verificación de cadena de dos certificados con root P-384 y firma JWS P-256 real; prueba fallaba antes y pasa tras convertir `R‖S` a DER. También se normalizan saltos de línea escapados del PEM.
- `/ready` ahora rechazará rol con bypass RLS, permisos DB incompletos, worker vivo con modo durable apagado, APNs sin worker/credenciales válidas y certificado Apple ausente/ilegible. Añadí migración 014 para validar/otorgar permisos al rol runtime y tests de regresión.
- Apliqué migraciones 001–014 en Postgres temporal local con `reciapp_runtime` no-superuser/no-BYPASSRLS; `/ready` local pasó. Suite server: `300 passed, 2 skipped`; compilación Python y `git diff --check` pasan. Harness iOS previo: `51 passed`; Swift modificado parsea. Build app nuevo no verificado por SwiftPM; producción y iPhone intactos.
- Aceptación producción/iPhone sigue pendiente: crear/usar runtime role y aplicar 012–014, poner root Apple PEM en API y worker, cambiar `DATABASE_URL`, alinear imagen/`WORKER_ENABLED`, desplegar API/worker, confirmar `/ready`, y probar restore/lista/recetas/APNs en iPhone. No se desplegó ni publicó.
