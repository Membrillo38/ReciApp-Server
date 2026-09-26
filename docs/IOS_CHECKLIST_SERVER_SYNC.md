# Checklist iOS ↔ server (VPS)

Server en Coolify `main`. Margen Pro **40%**, reserva job **50¢**, OCR último recurso.  
API: `https://51-255-43-100.sslip.io`  
Free: **3 miss / año**. Pro fair-use: budget = precio×0.60.

Usa esto como lista de huecos en la app. Lo ya OK se marca.

---

## Ya alineado (server + iOS base)

- [x] `AppConfig.apiBaseURL` → VPS `51-255-43-100.sslip.io` (no Render).
- [x] Superwall `identify` + attribute **`user_id`** (UUID backend). Server acepta también legacy `supabase_user_id`.
- [x] Paywall en `FREE_WEEKLY_LIMIT` / `FREE_YEARLY_LIMIT` (= 3/año en prod).
- [x] UI fair-use en `PRO_FAIR_USE_LIMIT`.
- [x] `warmUpBackend()` antes de auth.
- [x] Poll job / refresh `/v1/me` tras compra.
- [x] Prod `/health` + `/ready` responden 200 (`environment=production`); el `/ready` desplegado aún no comprueba la migración 008. Ver [auditoría de integración](2026-09-26-app-server-integration-audit.md).
- [ ] Webhook Superwall VPS: endpoint/source existen, pero entrega real no validada; configuración dashboard contradice un documento que registra `0 active endpoints`. Probar compra/restauración sandbox y confirmar `/v1/me` → `is_pro=true`.
- [x] Server códigos job canónicos (`link_in_bio`, `extraction_retryable`, carousel, etc.).
- [x] Server emite `SPEND_LIMIT` (403) cuando budget OpenAI se agota.

---

## Estado verificado en la fuente iOS

### 1. `SPEND_LIMIT` (403) — implementado; no desplegado

Server puede devolver:

```json
{"detail":{"code":"SPEND_LIMIT","message":"Usage budget reached.","reason":"…"}}
```

La app lo clasifica como `showSpendLimit`, no abre Superwall y conserva el enlace para reintentar. Build local validado; no se ha probado con cuenta real.

Archivo: `ReciApp/Services/ClientStatePolicy.swift` (+ mensaje en `Models.swift` / strings).

### 2. Copy cuota Free — implementado en la fuente

Server real: **3 miss / año** (`FREE_YEARLY_LIMIT`; la app aún acepta `FREE_WEEKLY_LIMIT` como alias legacy).

La app muestra **3 / año** y acepta `FREE_WEEKLY_LIMIT` como código heredado. Parte del copy de recuperación aún usa fallback inglés en idiomas sin traducción.

### 3. Errores de job (extract) — contrato estructurado implementado

Server guarda mensajes canónicos en inglés (localiza en API cuando aplica):

| Mensaje / sentido | Código interno server |
|---|---|
| Recipe link in bio… | `link_in_bio` |
| Incomplete TikTok carousel… | `incomplete_carousel` |
| Missing ingredient list / steps | `recipe_no_ingredients` / `recipe_no_method` |
| Extraction temporarily failed. Retry… | `extraction_retryable` |
| Video too long… | `video_too_long` |
| Unsupported URL… | `unsupported_url` |

El servidor ahora devuelve `error_code`; la app muestra el mensaje localizado, ofrece **Reintentar** para `extraction_retryable`/`stale_job` con un ID nuevo y no reintenta automáticamente los errores permanentes como `link_in_bio`.

### 4. QA post-cambio server

1. **Caption completa** (TikTok/IG) → OK sin tardar mucho (sin OCR).
2. **“Recipe in bio”** → error bio, **sin** gasto raro / sin spinner eterno.
3. **Carrusel** con caption incompleta → puede OCR slides; si falla mid-way, mensaje carousel incompleto.
4. **Video sin caption** → local STT primero; si local habla bastante, no debería ir a OpenAI STT.
5. **Mismo enlace otra vez** → cache hit, no cuenta cuota Free.
6. Free: 3 miss nuevos en el año → paywall. El 4.º falla con `FREE_YEARLY_LIMIT`.
7. Pro: tras mucho uso OpenAI del mes (budget = precio×0.60) → `PRO_FAIR_USE_LIMIT`, no paywall de compra.

### 5. Superwall dashboard

- Webhook solo VPS (ya).
- No apuntar a ningún `*.onrender.com`.
- Attribute en eventos: `user_id`.

### 6. Docs iOS a sync

- [x] Copy Free = **3 / año** en UI.
- [ ] Traducir todos los mensajes de recuperación en los idiomas restantes; hoy varios usan fallback inglés.

### 7. Opcional (no bloquea)

- Renombrar comentarios “weekly limit” → “yearly free cap” en UI/código.
- Analytics: event cuando `SPEND_LIMIT`.

---

## Qué no tocar en iOS

- No hace falta cambiar URL de API (ya VPS).
- No hace falta `supabase_user_id` (ya usáis `user_id`).
- No hace falta lógica OCR/STT en cliente: eso es solo server.

---

## Smoke mínimo prod

1. Sign in with Apple.
2. Import 1 TikTok/IG video con caption rica.
3. Import 1 “recipe in bio” → mensaje correcto.
4. Re-import mismo link → instant / cache.
5. Settings → Upgrade / restore → `/v1/me` `is_pro`.
6. Free: quemar cuota (3) y ver paywall `free_limit_reached`.

Si algo de la lista 1–3 no está, prioriza **`SPEND_LIMIT`** y **mostrar `job.error`**.
