# GeoInformesChile.cl

Mapa ciudadano, gratuito y no oficial de las alertas de SENAPRED, con videos
públicos por zona. Un archivo HTML, dos scripts de recolección, cero framework,
cero costo de operación (salvo el dominio).

**Principio del proyecto: gratis para las personas, siempre.** Si algún día hay
que financiarlo, pagan instituciones (embeds para medios, alertas por comuna
para municipios o faenas), nunca el ciudadano.

## Arquitectura

```
telegram_sae.py ──► telegram_alertas.jsonl ─┐
endpoint web SENAPRED ──────────────────────┼──► colector_senapred.py ──► alerts.json + historico.jsonl
capas ArcGIS SENAPRED ──────────────────────┘                                   │
                                            media_youtube.py ──► media.json     │
                                                          └────────────► index.html (Leaflet, single-file)
```

Todo corre por cron (GitHub Actions cada 15 min, `recolector.yml`) y se sirve
estático (GitHub Pages / Cloudflare Pages) con el dominio GeoInformesChile.cl.

## Los 4 enchufes que faltan (en orden)

1. **Endpoint web de SENAPRED** — `senapred.cl/eventos` es una SPA React.
   Ábrela con DevTools → Network → Fetch/XHR, recarga, y pega la URL del JSON
   de alertas en `WEB_ENDPOINT` de `colector_senapred.py`. Si el mapeo de
   campos no calza, el script imprime las llaves reales del primer registro
   para ajustar `normalizar()`.
2. **Capas ArcGIS** — abre
   `https://www.arcgis.com/sharing/rest/content/items/bdc345e01e324af490800634d0f0e3a5/data?f=json`
   (config del dashboard público "ALERTAS SENAPRED") y copia toda `url` que
   termine en `/FeatureServer/<n>` a `ARCGIS_LAYERS`. Es la fuente de respaldo.
3. **Telegram** — crea credenciales en `my.telegram.org` (TG_API_ID /
   TG_API_HASH en `.env`), corre `python telegram_sae.py` UNA vez local para el
   login (genera `geoinformes.session`) y guarda la session como secret en
   base64 para Actions. Esto captura SAE, declaraciones y cancelaciones del
   canal público t.me/SenapredChile.
4. **comunas.geojson** — descarga la división comunal (BCN / IDE Chile en
   geoportal.cl) y simplifícala para la web:
   `npx mapshaper comunas.shp -simplify 5% keep-shapes -proj wgs84 -o format=geojson precision=0.0001 comunas.geojson`
   Ajusta `propComuna` / `propRegion` en `index.html` si tus propiedades se
   llaman distinto.

Opcionales: `YT_API_KEY` (Google Cloud Console → YouTube Data API v3) activa
los videos por zona; `ANTHROPIC_API_KEY` activa el re-ranking LLM que bota
falsos positivos. Sin esas llaves, el mapa de alertas funciona igual.

## Frontend

- Leaflet **local, sin CDN**: descarga Leaflet 1.9.x y deja `leaflet.js` y
  `leaflet.css` (con su carpeta `images/`) en `lib/`.
- Teselas: OSM estándar para partir. Si el sitio agarra tráfico de evento
  nacional, cambia a un proveedor de teselas con límites contratados: el tile
  server público de OSM no es para sitios de alto tráfico.
- `reportarURL` en la CONFIG del `index.html`: a dónde llega el botón
  "Reportar un video" (issue de GitHub o formulario, a tu gusto).

## Reglas no negociables

- Disclaimer visible siempre: sitio referencial, sin afiliación a SENAPRED; en
  emergencias mandan los canales oficiales y la mensajería SAE.
- Nada de logos ni estética gubernamental. Cita "Fuente: SENAPRED" en el pie.
- Todo video se muestra como **no verificado**, con timestamp de publicación y
  filtro `publishedAfter` desde el inicio del evento. Mostrar un video
  reciclado de otro año como "en vivo" destruye el proyecto.
- Timestamp de "última actualización" y semáforo de fuentes siempre a la
  vista: la confianza es el producto.

## Costos

$0 de operación en el MVP: Actions + Pages + API de Telegram + cuota gratis de
YouTube. Pagado: el dominio en NIC Chile y, si activas el LLM, centavos por
corrida. X (búsqueda de videos) queda para fase 2 por decisión de diseño.
