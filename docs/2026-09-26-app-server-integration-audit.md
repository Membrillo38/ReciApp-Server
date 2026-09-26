# Auditoría ReciApp iOS ↔ servidor

Fecha: 2026-09-26
Repositorios revisados: `ReciApp-iOS` y `ReciApp-Server`.

## Resultado

En las fuentes locales, los contratos principales de login, perfil, biblioteca, detalle, importación, cola, traducción, borrado y errores coinciden entre Swift y FastAPI. En producción, una consulta directa actual devuelve ausentes la columna e índice de migración 008 (`false/false`), necesarios para imports idempotentes. `/ready` sigue devolviendo 200 porque la versión desplegada aún no hace la comprobación estricta del checkout revisado; los imports nuevos de ShareInbox no pueden completar hasta aplicar 008 y desplegar el backend corregido. La app compila para iOS Simulator; faltan pruebas autenticadas en un iPhone.

## Cambios hechos en esta revisión

- `GET /v1/me` ahora devuelve `free_used_this_year` además del campo histórico `free_used_this_week`. Ambos representan el mismo contador anual durante la transición.
- El mensaje de límite preventivo de la app ahora dice “por año”, que coincide con la cuota real de tres importaciones nuevas al año UTC.
- La renovación del refresh token ahora revoca el token anterior e inserta el siguiente en una única sentencia transaccional. Antes eran operaciones separadas: si fallaba el insert después de revocar, se podía dejar al usuario sin token válido.
- El borrado de cuenta ahora limpia biblioteca, trabajos y accesos compartidos, ledger y eventos de seguridad, revoca sesiones y cierra el perfil en una sola transacción. Antes se ignoraban errores de limpieza y se podía responder éxito dejando datos ligados a la cuenta. Un fallo DB ahora responde `503 ACCOUNT_DELETION_UNAVAILABLE`; el cliente conserva la sesión para reintentar. Cada URL de trabajo se anonimiza con un valor distinto por ID, para no chocar con el índice único de imports activos. También cancela trabajos pendientes y `save_user_recipe` ya no puede volver a vincular una receta si un worker termina tras cerrar la cuenta.
- Los errores transitorios de endpoints de autenticación y perfil ahora incluyen `detail.code/message` además del sobre superior histórico. El cliente Swift compartía el parser para ambos formatos y antes solo entendía el campo `detail`.
- Los trabajos fallidos ahora incluyen un `error_code` estable. iOS ofrece reintento manual para `extraction_retryable` y `stale_job`, con un nuevo ID de entrega; errores permanentes como `link_in_bio` no reintentan en bucle.
- Un fallo temporal al adjuntar una receta cacheada o recién completada a la biblioteca ahora devuelve `503` con `Retry-After`. Repetir el mismo ID de entrega o sondear el mismo job vuelve a intentar el enlace, en vez de responder éxito dejando la receta invisible.
- Endurecí `/ready`: no basta con que PostgreSQL responda. Comprueba que `client_delivery_id` sea UUID y que el índice de idempotencia exista, sea único, válido y listo, y use las columnas y predicado correctos. Así una migración parcial o un índice inválido con el mismo nombre no anuncia que el backend puede aceptar imports.
- El aviso de receta lista está traducido a los 46 idiomas no ingleses del catálogo. Sigue siendo una notificación local de mejor esfuerzo.
- Actualicé las pruebas del servidor que apuntaban a APIs antiguas del extractor/STT, límites y precios anteriores; mantienen la intención de comprobar los casos actuales.
- Añadidas pruebas unitarias para la rotación atómica del refresh token y el contador anual de perfil.
- Añadí cobertura iOS para respuestas HTTP 500/502: conserva el import y permite recuperarlo sin repetir automáticamente el `POST` incierto.
- ShareInbox guardaba la entrega, pero repetía errores transitorios del extractor cada 2 segundos. Ahora aplica backoff exponencial hasta 60 segundos, respeta `Retry-After` cuando fija un mínimo y limpia el estado al completar o reintentar manualmente. Verificado con 46 pruebas del `ClientStateHarness` y build iOS Simulator.
- La configuración de producción versionada arrancaba solo FastAPI con `WORKER_ENABLED=false`, así que el procesamiento dependía de `BackgroundTasks` en memoria. Preparé un servicio `recipe-worker` que reclama trabajos durables de PostgreSQL y activa el modo worker también en el API. Está en el commit local `1de66d1` de `codex/reciapp-durable-worker`; el repo de infraestructura no tiene remoto Git configurado, por lo que no pude publicarlo. No está desplegado; falta confirmar esquema y hacer rollout controlado.

## Contratos comprobados

- Apple login, refresh y logout: campos `identity_token`, `nonce`, `full_name`, `authorization_code`, `refresh_token` y respuesta de sesión compatibles.
- Perfil: `/v1/me`; biblioteca: `/v1/me/recipes`; detalle: `/v1/recipes/{id}`.
- Importación: `POST /v1/extract`; seguimiento: `/v1/me/jobs` y `/v1/jobs/{id}`; la app persiste y sigue `next_job_id`.
- Borrado de receta y cuenta: `DELETE /v1/me/recipes/{id}` y `DELETE /v1/me`.
- El borrado de cuenta es idempotente y atómico dentro de PostgreSQL; mantiene `usage_events` para preservar cuota anual tras reactivar la misma identidad Apple.
- El cliente respeta `Retry-After`, distingue 401/403/404/422/429/503 y evita repetir automáticamente un POST de extracción cuyo resultado sea incierto.
- El identificador de entrega `client_delivery_id` del cliente requiere la columna creada por `migrations/008_extract_delivery_idempotency.sql`.
- Pro depende de Superwall: iOS identifica al usuario con su UUID y FastAPI valida `Svix` en `/v1/webhooks/superwall`. La consulta de producción confirma recepción/procesamiento reciente y secreto configurado; dos documentos operativos discrepan sobre la configuración del endpoint y deben actualizarse. Falta una compra/restauración sandbox controlada seguida del `GET /v1/me` de esa cuenta.

## Riesgos pendientes

### Migración 008 de imports: falta en producción

Observaciones de solo lectura en producción, 2026-09-26:

- Reconsulta pública actual: `/health` HTTP 200 (`status=ok`) y `/ready` HTTP 200 (`status=ready`, `environment=production`, `maintenance=false`, latencia 2 ms). La respuesta no informa versión ni qué esquema valida.
- En la inspección anterior, la API desplegada aceptaba `client_delivery_id` y `OPENAI_API_KEY` estaba configurada; no revalidé esas dos condiciones en el probe actual.
- La introspección PostgreSQL directa de esta reconsulta devolvió `false/false`: siguen ausentes `extract_jobs.client_delivery_id` y `extract_jobs_user_delivery_unique`.
- Conteo de la ventana reciente de 24 h: 965 registros; no aparecen marcadores `request completed`, etapas/fallos de extracción, errores upstream, rate limits ni líneas `ERROR`/`WARNING`. Al faltar marcadores de request, los logs no permiten estimar tráfico ni tasas 4xx/5xx; cero coincidencias no prueba que no hubiera solicitudes.

El probe público de `/ready` devuelve 200 mientras la consulta directa confirma que faltan columna e índice; por tanto, la versión desplegada no está ejecutando la comprobación estricta del checkout actual. El cliente envía `client_delivery_id` y la API consulta esa columna para deduplicar, así que los imports nuevos desde ShareInbox no pueden completar hasta aplicar 008. El código local de `/ready` valida ambos objetos y devuelve 503 si falta alguno.

La migración 008 solo añade una columna nullable y un índice parcial único; no modifica filas existentes. No se aplicó en producción durante esta revisión.

### Las notificaciones de importación no son push del servidor

La app programa una notificación local solo cuando su sondeo detecta que terminó la receta mientras está en background. `beginBackgroundTask` es temporal; iOS puede suspender o cerrar la app antes de terminar el sondeo. El servidor no registra tokens APNs ni envía notificaciones. El aviso no está garantizado al cerrar la app o dejarla suspendida durante una importación larga. Al volver a abrirla, la app retoma el sondeo y muestra el resultado. El aviso de receta lista tiene 46 idiomas; fuera de ese aviso, el catálogo conserva 28 claves con cobertura parcial (entre 2 y 3 idiomas) y 8 claves sin traducciones. Incluye errores de login, recuperación, permisos y ajustes de medida; esas cadenas pueden salir en inglés.

### Worker de extracción: cambio de infraestructura preparado, no desplegado

La configuración Compose inspeccionada tenía solo `recipe-backend`, sin worker separado; `WORKER_ENABLED` usa `false` por defecto y la inspección remota no encontró contenedor `recipe-worker`. Si el proceso API se reinicia durante una extracción, FastAPI puede perder la tarea en memoria y la fila queda para recuperación posterior. Preparé `recipe-worker` en el commit local `1de66d1` de `codex/reciapp-durable-worker`, con el mismo image/env, acceso privado a Postgres y leases. El repo de infraestructura no tiene remoto Git configurado, así que el commit sigue local. La red `reciapp-internal` permite salida (`internal=false`). `/ready` comprueba la base y el esquema, no que el worker esté vivo; después del rollout también hay que verificar el contenedor y su log de arranque.

### Pro en producción: webhook recibe eventos; falta compra/restauración controlada

La integración de código está: `SubscriptionService.identify()` envía el UUID backend como `user_id`; `/v1/webhooks/superwall` exige firma Svix y actualiza `profiles.is_pro`; la app refresca `/v1/me` tras compra/restauración. Verifiqué sin leer secretos que el secreto del webhook está configurado. En `subscription_events` hay 7 eventos Superwall en los últimos 30 días: 7 procesados, 0 fallidos (3 eventos Pro-on, 2 Pro-off y 2 cambios de estado). Esto confirma recepción y procesamiento real del webhook. Sigue sin probarse una compra/restauración sandbox completa en un dispositivo junto con el `GET /v1/me` de esa misma cuenta.

### Pruebas y producción

- Compilación iOS Simulator: correcta (`xcodebuild`, Debug, sin firma).
- Pruebas Swift del `ClientStateHarness`: 46 pasaron.
- Suite servidor + contratos cruzados iOS: 240 pasaron, 2 omitidas deliberadamente porque están reemplazadas por tests de comportamiento del ClientStateHarness. Se comprueban campos `free_used_this_year`, `client_delivery_id` y `error_code` en Swift/FastAPI. Los tests cruzados antes se omitían por una ruta fija `IosAPP/`; ahora localizan el checkout hermano `ReciApp-iOS` o `RECIAPP_IOS_ROOT`. El entorno local no tiene `sentry_sdk`; para ejecutar suite se usa shim temporal sin cambios en el repositorio, así que telemetría Sentry no queda validada.
- Xcode Debug unsigned Simulator: build actual correcto. ClientStateHarness: 46 tests pasaron. `simctl`/`devicectl` no pudieron iniciar CoreSimulator/CoreDevice, así que no hubo ejecución visual ni prueba firmada en dispositivo.
- Consulté el SQL real de readiness en un PostgreSQL temporal aislado: migración 008 correcta → `delivery_column=t`, `delivery_index=t`; índice no único y mal definido con el mismo nombre → `delivery_index=f`.
- No pude consultar issues actuales de Sentry: no hay `SENTRY_AUTH_TOKEN` local configurado. No se leyó ni compartió ningún token.
- `/health` y `/ready` públicos respondieron HTTP 200 en la última consulta. La respuesta pública de `/ready` no identifica qué build ni qué comprobaciones ejecuta, así que no confirma por sí sola el estado de la migración.
- No se probó Apple login ni una extracción real en dispositivo; tampoco la compra/restauración sandbox de extremo a extremo, aunque sí hay webhooks Superwall procesados en producción.
- Los cambios de API/app están en las ramas remotas `codex/reciapp-server-integration` y `codex/reciapp-ios-integration`; la configuración del worker está en un commit local de `codex/reciapp-durable-worker` porque el repo ops no tiene remoto. Ninguno se ha desplegado en producción ni publicado en App Store.

## Siguiente orden de aceptación

1. Aplicar migración 008 en producción y desplegar backend con readiness de esquema; comprobar `/ready`.
2. Verificar configuración real del webhook Superwall y compra/restauración sandbox contra `/v1/me`.
3. Publicar el cliente que envía `client_delivery_id` solo después del paso 1.
4. Con una cuenta de prueba: Apple login, renovación, perfil Pro/free, biblioteca, detalle traducido, URL nueva/cacheada, cola, reanudación, rate limit y borrado.
5. Repetir esa matriz en iPhone con compilación firmada y comprobar Wi-Fi y red móvil.
