# Integración de ReciApp en Swift

Esta guía describe el contrato del servidor y cómo conectarlo a una app SwiftUI. El cliente de referencia completo está en [`docs/swift/ReciAppAPI.swift`](docs/swift/ReciAppAPI.swift). Se ha comprobado con el compilador en modo Swift 6; requiere iOS 16 o posterior. La app usa JWT propios del backend: recibe una función que obtiene o renueva el token.

## 1. Configuración

- API: `https://51-255-43-100.sslip.io`
- Autenticación de la API: `Authorization: Bearer <access_token del backend>`.
- Nunca incluyas `service_role`, `API_KEY` administrativa, OpenAI ni secretos de webhooks en la app.

En la app existente, adapta `Services/AuthService.swift` y `Services/APIClient.swift`; no añadas modelos con nombres duplicados. La app iOS vive fuera de este repo: `/Users/andrescasillas/Desktop/ReciApp-iOS`.

## 2. Autenticación y cliente

Crea una única sesión local con `access_token`, `refresh_token`, expiración y usuario. Guarda tokens en Keychain. Inicializa el cliente de referencia así:

```swift
let api = ReciAppAPI { refresh in
    if refresh {
        return try await authService.bearerToken(refresh: true)
    }
    return try await authService.bearerToken(refresh: false)
}
```

Para Sign in with Apple, activa la capability. Genera un nonce aleatorio para cada intento; envía su SHA-256 a Apple y el nonce original al backend junto al ID token. No reutilices un nonce ni uses el authorization code como ID token.

```swift
// appleIDToken, authorizationCode y rawNonce proceden del flujo Apple completado.
let body: [String: String] = [
    "identity_token": appleIDToken,
    "nonce": rawNonce,
    "full_name": fullName,
    "authorization_code": authorizationCode // opcional; omítelo si servidor/Apple no están configurados
]
// Envía body a POST /v1/auth/apple y guarda la sesión devuelta en Keychain.
```

El source actual de `POST /v1/auth/apple` acepta `authorization_code` opcional además de `identity_token`, `nonce` y `full_name`. Antes de publicar el cliente, confirma que producción tenga ese backend; si no, omite el campo. Con el backend y la configuración Apple listos, envía `ASAuthorizationAppleIDCredential.authorizationCode` para que servidor intercambie y guarde cifrado el refresh token de Apple. El login sigue siendo válido si el code falta o el intercambio falla.

`POST /v1/auth/apple` devuelve `access_token`, `refresh_token`, `token_type`, `expires_in` y `user`. Renueva con `POST /v1/auth/refresh`; cierra con `POST /v1/auth/logout` de forma best-effort y borra Keychain siempre. No registres tokens, contraseñas ni cuerpos de respuestas privadas.

El cliente renueva sesión y repite una sola vez ante HTTP 401. No repite automáticamente escrituras ante timeout o HTTP 5xx, porque podrían haberse ejecutado. La sesión predeterminada del cliente rechaza redirecciones para no reenviar credenciales fuera del endpoint esperado. Si inyectas otra URLSession, conserva esta protección.

## 3. Biblioteca y detalle

```swift
let profile = try await api.me()
let library = try await api.recipes(language: "es-ES")
let detail = try await api.recipe(id: recipeID, language: "es-ES")
```

Carga perfil y biblioteca de forma independiente. Un error al obtener el plan no debe ocultar recetas ya guardadas. Al tocar una receta, presenta inmediatamente el detalle con skeleton y después sustituye sus datos. Publica cambios de interfaz en `@MainActor`.

Los campos `ingredientSections`, `tips` y `carouselImageUrls` llegan como arrays, incluso vacíos. Conserva el orden de las imágenes. Si no hay carrusel, usa `thumbnailUrl`. Si no hay secciones, muestra `ingredients`. Ordena pasos por `order`; muestra consejos al final.

`languageCode` indica el idioma realmente devuelto. Listado y detalle pueden devolver la versión original si la traducción aún no existe; no prometas que el parámetro `language` por sí solo la genera.

## 4. Importación y recuperación

Idiomas canónicos: los 50 identificadores de App Store (`ar`, `bn`, `ca`, `zh-Hans`, `zh-Hant`, `hr`, `cs`, `da`, `nl`, `en-AU`, `en-CA`, `en-GB`, `en-US`, `fi`, `fr-FR`, `fr-CA`, `de`, `el`, `gu`, `he`, `hi`, `hu`, `id`, `it`, `ja`, `kn`, `ko`, `ms`, `ml`, `mr`, `nb`, `or`, `pl`, `pt-BR`, `pt-PT`, `pa`, `ro`, `ru`, `sk`, `sl`, `es-MX`, `es-ES`, `sv`, `ta`, `te`, `th`, `tr`, `uk`, `ur`, `vi`). Alias de dispositivo (`ar-SA`, `de-DE`, `zh-TW`, `no`, …) se normalizan al canónico; valores desconocidos caen a `en-US`.

```swift
let started = try await api.extract(url: sourceURL, language: "es-ES")
// Guarda started.jobId + idioma + userID ANTES de comenzar el polling.
let recipe = try await api.waitForRecipe(
    jobID: started.jobId,
    language: "es-ES"
) { state in
    // Guarda state.nextJobId ?? state.jobId. Actualiza progreso en MainActor.
}
```

El servidor acepta URLs públicas de TikTok, YouTube, Instagram y Facebook. Descargas privadas, contenido eliminado, restricciones de plataformas o falta de información culinaria pueden acabar en un job fallido.

- `POST /v1/extract` devuelve `job_id`, `status`, `cache_hit`, `progress`, y opcionalmente `queued` / `queue_position`.
- Si el usuario ya tiene extracts `pending`/`processing`, el servidor **encola** el nuevo job (`queued=true`) en vez de rechazarlo con 429 (salvo tope de 20 o rate limit).
- La cola es **serial por usuario**: un extract activo; al terminar, el proceso arranca el `pending` más viejo.
- `GET /v1/me/jobs` lista extracts abiertos (oldest first) con `queue_position`, `source_url`, `progress`, `status`. Úsalo para “Receta 2 de 5”.
- También en cache hit, consulta el job para obtener la receta.
- `GET /v1/jobs/{id}?language=es-ES` devuelve `pending`, `processing`, `completed` o `failed`.
- Sigue siempre `next_job_id ?? job_id` de la respuesta: una extracción puede continuar con otro job de traducción.
- Guarda el job y su idioma por usuario para reanudar al volver al foreground o reiniciar la app.
- El cliente espera cada 2 segundos y termina tras aproximadamente 10 minutos más la petición en curso. Cancelar la tarea o agotar la espera no cancela el trabajo del servidor.
- Ante error transitorio al consultar un job, ofrece reanudar ese ID. No vuelvas a crear una extracción automáticamente.
- Una receta fallida puede aparecer en el payload de una traducción: comprueba primero `status`, después `recipe`.
- Pegar o compartir **varios enlaces** debe encolar todos (cap 20). No marques un share como procesado hasta que `POST /v1/extract` cree el job (o cache hit).
- **Dedupe local:** antes de `POST /v1/extract`, si esa URL (normalizada) ya está en la biblioteca del usuario, no reenvíes salvo “reimportar”. Cache hit en servidor es barato pero aún crea job; skip local evita round-trip y ruido.
- Free plan: permite 3 misses nuevos por año civil UTC; el 4.º puede devolver 403 `FREE_YEARLY_LIMIT` (paywall). `FREE_WEEKLY_LIMIT` solo se acepta como alias heredado. Los jobs ya aceptados siguen; cache hit no gasta cupo.

Antes de desplegar el cliente actual, aplica `migrations/008_extract_delivery_idempotency.sql` y `migrations/009_refresh_rotation_replay.sql`; la API actual requiere ambas para readiness e idempotencia de imports/refresh. Confirma `/ready` tras aplicar esquema.

Con `WORKER_ENABLED=true` el proceso web solo admite HTTP/poll; el servicio worker de `docker-compose.worker.yml` ejecuta extracts. No actives el flag en Coolify sin worker en marcha (jobs quedarían `pending`).

## 5. Errores y suscripciones

La propiedad JSON `detail` puede ser texto, un objeto con `code` y `message`, o una lista de errores de validación. El cliente de referencia acepta estas formas y conserva `X-Correlation-ID` y `Retry-After` en `ReciAPIError`.

| Estado/código | Comportamiento en la app |
| --- | --- |
| 401 | Renovar sesión una vez; después solicitar login. |
| 403 `FREE_YEARLY_LIMIT` | Presentar paywall. Aceptar `FREE_WEEKLY_LIMIT` como alias heredado. |
| 403 `PRO_FAIR_USE_LIMIT` | Mostrar límite temporal de uso. |
| 403 `ACCOUNT_DELETED` | Cuenta cerrada. Cerrar sesión, borrar cachés y mostrar login. No reutilizar el JWT. |
| 403 `ACCOUNT_UNAVAILABLE` | Perfil ausente o no usable. Cerrar sesión y volver a Sign in with Apple. |
| Otro 403 | Mostrar falta de acceso; no abrir paywall automáticamente. |
| 404 | Job/receta inexistente o eliminada. |
| 413 / 422 | Corregir entrada; no repetir sin cambios. |
| 429 | Esperar; respetar `Retry-After` si está presente. |
| 503 | Conservar datos y permitir reintento; puede indicar mantenimiento o dependencia temporalmente caída. |
| Job `failed` con HTTP 200 | Mostrar `error`; el transporte funcionó pero la importación no. |

En la implementación actual, un cache hit ya localizado no consulta cuota ni llama OpenAI, aunque registra `extract_hit`. Las nuevas extracciones y traducciones sí aplican cuota y reserva de gasto. `free_remaining` puede ser cero y aun así una receta ya cacheada estar disponible. No bloquees todas las importaciones desde la UI basándote solo en ese campo.

Para Superwall, configura la clave pública del proyecto, identifica al usuario con su UUID del backend y asigna el atributo `user_id` con el mismo UUID. Tras compra o restauración, refresca `/v1/me` con espera acotada: el webhook puede llegar después que el callback de StoreKit. El servidor es quien confirma `is_pro`; no lo sobrescribas desde el cliente. La integración de pago real requiere probar compra sandbox y webhook firmado.

## 6. Share Extension y eliminación

Comparte URL e idioma mediante App Group `group.com.membri.reciapp`; habilítalo en ambos targets y sus perfiles de firma. La Share Extension debe encolar **todos** los adjuntos compatibles (no solo el primero) y abrir la app una vez. Conserva enlaces recibidos sin sesión hasta terminar login. Deduplica entregas para no lanzar dos POST. La app principal debe crear el job y guardar su ID; la extensión no debe depender de ejecutar una extracción larga. Si llega un segundo share mientras importa, encola y haz `POST /v1/extract` (quedará `pending`); no descartes el delivery.

`removeRecipe(id:)` elimina la asociación del usuario, no la caché global. `deleteAccount()` solicita eliminar la cuenta; después del éxito, cierra sesión y borra cachés locales. Presenta confirmación explícita en la UI para borrar la cuenta. El servidor revoca el refresh token de Apple si lo tiene, borra recetas y tokens de la app, y marca el perfil como cerrado. Conserva `apple_sub` y el historial de uso: el mismo Apple ID reactiva el mismo perfil y no reinicia el cupo free anual. Un segundo `DELETE /v1/me` con el mismo JWT es idempotente (`ok: true`). Cuentas antiguas sin token de Apple guardado no pueden revocarse en Apple: conserva el fallback de recuperación manual hasta que esas sesiones caduquen.

## 7. Pruebas de aceptación de la app

1. Login Apple y renovación tras 401; comprobar que ningún secreto del servidor aparece en el bundle.
2. Perfil, biblioteca y detalle con una cuenta real; conservar biblioteca si falla perfil.
3. Importar URL nueva y URL cacheada; comprobar gasto y cuota solo según respuesta del servidor.
4. Seguir cambio de job a traducción y comprobar `languageCode` final.
5. Cerrar y abrir la app durante polling; reanudar el job guardado.
6. Share Sheet con app abierta, cerrada y sin sesión.
7. Red desconectada, HTTP 429/503, entrada inválida y job fallido.
8. Compra/restauración sandbox con webhook y actualización real de `/v1/me`.

Para verificar el backend con una sesión de prueba existente, configura `RECIAPP_ACCESS_TOKEN` mediante un mecanismo local seguro y ejecuta:

```bash
export RECIAPP_EXPECTED_API_HOST=51-255-43-100.sslip.io
python scripts/authenticated_readiness.py \
  --base-url https://51-255-43-100.sslip.io --cycles 100
```

El script solo imprime métricas y estados; comprueba 100 ciclos de biblioteca/perfil y exige p95 inferior a 800 ms. `/health` y `/ready` son comprobaciones públicas: no prueban por sí mismas que login, extracción y facturación funcionen de extremo a extremo.
