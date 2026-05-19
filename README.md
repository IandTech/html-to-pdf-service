# html-to-pdf-service

Servicio REST en Python para convertir HTML completo a PDF usando FastAPI + Playwright + Chromium, listo para desplegar en Render con Docker. Está pensado para flujos de Power Automate que envían el HTML completo de un correo Trade Ticket y necesitan un PDF visualmente fiel con imágenes, estilos, tablas, fondos y firmas.

## Características

- FastAPI async con endpoint de conversión binaria `application/pdf`
- Renderizado real con Chromium headless vía Playwright
- Espera de carga de recursos con `wait_until="networkidle"`
- PDF A4 con fondos impresos y márgenes de 10mm
- Browser global en startup y cierre ordenado en shutdown
- Una página aislada por request
- Sin persistencia de archivos en disco
- CORS básico habilitado
- Logging estructurado en JSON
- Errores explícitos con `traceId` para soporte operativo

## Estructura del proyecto

```text
.
├── .dockerignore
├── Dockerfile
├── README.md
├── main.py
├── render.yaml
└── requirements.txt
```

## Endpoints

### `GET /`

Respuesta:

```json
{
  "service": "html-to-pdf-service",
  "status": "ok",
  "version": "1.0.0"
}
```

### `GET /health`

Respuesta:

```json
{
  "status": "healthy",
  "browser": "connected",
  "environment": "render"
}
```

### `POST /convert/html-to-pdf`

Request:

```json
{
  "html": "<html><body><h1>Trade Ticket</h1></body></html>",
  "fileName": "trade-ticket.pdf",
  "metadata": {
    "source": "PowerAutomate",
    "ticketId": "TT-001",
    "emailSubject": "Trade Ticket 001"
  }
}
```

Respuesta exitosa:

- `Content-Type: application/pdf`
- Body binario del PDF
- Header `Content-Disposition: attachment; filename="trade-ticket.pdf"`

### `POST /convert/email-to-pdf`

Endpoint único para convertir archivos `.eml` o `.msg` a PDF.

Request:

- `Content-Type: multipart/form-data`
- Campo obligatorio `file`: archivo `.eml` o `.msg`
- Campo opcional `fileName`: nombre de salida del PDF
- Campo opcional `metadata`: JSON serializado como texto

Comportamiento:

- extrae el cuerpo HTML si existe
- si no existe HTML, usa el cuerpo de texto plano y lo convierte a HTML básico
- intenta resolver imágenes inline `cid:` embebiéndolas como `data:`
- reutiliza el mismo motor Chromium del endpoint HTML

## Variables de entorno opcionales

- `RENDER_TIMEOUT_MS`: timeout máximo en milisegundos para render y generación PDF. Default `30000`.
- `LOG_LEVEL`: nivel de logging. Default `INFO`.
- `STRICT_EXTERNAL_RESOURCES`: si es `true`, la API devuelve `422` cuando falla cualquier recurso externo. Default `false`.

## Ejecución local con Docker

### 1. Construir imagen

```bash
docker build -t html-to-pdf-service .
```

### 2. Ejecutar contenedor

```bash
docker run --rm -p 10000:10000 \
  -e RENDER_TIMEOUT_MS=30000 \
  -e LOG_LEVEL=INFO \
  -e STRICT_EXTERNAL_RESOURCES=false \
  html-to-pdf-service
```

### 3. Probar salud

```bash
curl http://localhost:10000/health
```

## Ejecución local sin Docker

Si quieres correrlo fuera del contenedor de Render, instala dependencias y los browsers de Playwright:

```bash
pip install -r requirements.txt
playwright install chromium
uvicorn main:app --host 0.0.0.0 --port 10000
```

## Ejemplos `curl`

### Health

```bash
curl http://localhost:10000/health
```

### Convertir HTML a PDF

```bash
curl -X POST http://localhost:10000/convert/html-to-pdf \
  -H "Content-Type: application/json" \
  --output trade-ticket.pdf \
  -d '{
    "html": "<html><body style=\"font-family: Arial;\"><h1>Trade Ticket</h1><p>Processed from email.</p><img src=\"https://via.placeholder.com/300x80\"></body></html>",
    "fileName": "trade-ticket.pdf",
    "metadata": {
      "source": "PowerAutomate",
      "ticketId": "TT-001",
      "emailSubject": "Trade Ticket 001"
    }
  }'
```

### Convertir usando archivo JSON

```bash
curl -X POST http://localhost:10000/convert/html-to-pdf \
  -H "Content-Type: application/json" \
  --output document.pdf \
  --data @payload.json
```

### Convertir `.eml` o `.msg` a PDF

```bash
curl -X POST http://localhost:10000/convert/email-to-pdf \
  -F "file=@correo.eml" \
  -F "fileName=correo-renderizado.pdf" \
  -F "metadata={\"source\":\"Postman\",\"ticketId\":\"TT-EMAIL-001\"}" \
  --output correo-renderizado.pdf
```

## Ejemplo Power Automate

Usa una acción HTTP con esta configuración:

- Método: `POST`
- URL: `https://<tu-servicio-render>/convert/html-to-pdf`
- Headers: `Content-Type: application/json`
- Body:

```json
{
  "html": "@{triggerOutputs()?['body/body']}",
  "fileName": "trade-ticket-@{utcNow('yyyyMMdd-HHmmss')}.pdf",
  "metadata": {
    "source": "PowerAutomate",
    "ticketId": "@{variables('ticketId')}",
    "emailSubject": "@{triggerOutputs()?['body/subject']}"
  }
}
```

Si luego necesitas adjuntar o guardar el PDF, consume el body binario de la respuesta HTTP en el siguiente paso del flujo.

## Despliegue en Render

### Opción 1. Desde GitHub con Dockerfile

1. Sube este proyecto a GitHub.
2. En Render crea un nuevo `Web Service`.
3. Conecta el repositorio.
4. Render detectará el `Dockerfile`.
5. Configura variables si quieres ajustar timeout o nivel de logs:
   - `RENDER_TIMEOUT_MS=30000`
   - `LOG_LEVEL=INFO`
   - `STRICT_EXTERNAL_RESOURCES=false`
6. Usa `/health` como health check.

### Opción 2. Usando `render.yaml`

Render puede aprovisionar el servicio automáticamente al detectar el archivo [`render.yaml`](/C:/TradeTicket_API/render.yaml).

## Errores posibles

Todos los errores devuelven JSON estructurado con `traceId`:

```json
{
  "success": false,
  "errorCode": "HTML_EMPTY",
  "errorType": "ValidationError",
  "message": "The HTML content received is empty.",
  "technicalDetail": "Request body html property was null or blank.",
  "timestamp": "2026-05-18T20:00:00Z",
  "traceId": "uuid-generated-id"
}
```

### `HTML_EMPTY` - HTTP 400

Se devuelve cuando `html` llega `null`, vacío o solo con espacios.

### `INVALID_HTML` - HTTP 400

Se devuelve cuando `html` no es string o no contiene markup HTML detectable.

### `PDF_RENDER_TIMEOUT` - HTTP 408

Se devuelve cuando Chromium excede el timeout configurado durante `set_content` o `pdf()`.

### `EXTERNAL_RESOURCE_LOAD_FAILED` - HTTP 422

Se devuelve cuando Playwright detecta uno o más recursos externos fallidos y `STRICT_EXTERNAL_RESOURCES=true`. La respuesta incluye:

```json
{
  "details": {
    "failedResourceCount": 2,
    "failedResources": [
      {
        "url": "https://example.com/logo.png",
        "method": "GET",
        "resourceType": "image",
        "errorText": "net::ERR_NAME_NOT_RESOLVED"
      }
    ]
  }
}
```

### `BROWSER_RENDER_ERROR` - HTTP 500

Se devuelve cuando Playwright o Chromium fallan al renderizar el documento.

### `EMAIL_FILE_EMPTY` - HTTP 400

Se devuelve cuando se sube un `.eml` o `.msg` vacío.

### `UNSUPPORTED_EMAIL_FILE_TYPE` - HTTP 415

Se devuelve cuando el archivo enviado no termina en `.eml` o `.msg`.

### `EMAIL_CONTENT_EXTRACTION_FAILED` - HTTP 422

Se devuelve cuando la API no puede parsear el archivo de correo.

### `EMAIL_BODY_NOT_FOUND` - HTTP 422

Se devuelve cuando el archivo sí se pudo abrir, pero no contiene cuerpo HTML ni texto plano renderizable.

### `INVALID_METADATA` - HTTP 400

Se devuelve cuando el campo `metadata` del multipart no es JSON válido o no es un objeto JSON.

### `UNEXPECTED_SERVER_ERROR` - HTTP 500

Se devuelve ante una excepción no controlada. El stack trace queda solo en logs internos y el cliente recibe un `traceId`.

## Logging esperado

Cada request genera logs JSON útiles para debugging, auditoría y soporte:

- `traceId`
- path y método
- tiempo de procesamiento
- tamaño del HTML recibido en bytes
- nombre final del archivo PDF
- cantidad de recursos externos detectados
- cantidad de recursos fallidos
- status final del request
- formato de archivo fuente cuando se usa `.eml` o `.msg`

Cuando `STRICT_EXTERNAL_RESOURCES=false`, los recursos externos fallidos se registran como advertencia no fatal. En ese caso:

- la API sigue devolviendo `200` con el PDF
- se agregan headers `X-External-Resources-Status: warning` y `X-Failed-Resource-Count`
- el status final en logs será `SUCCESS_WITH_RESOURCE_WARNINGS`

Ejemplo:

```json
{
  "timestamp": "2026-05-19T14:00:00Z",
  "service": "html-to-pdf-service",
  "event": "request_completed",
  "traceId": "6c1f2d10-6f0f-4ff0-99b1-f2992ed58083",
  "method": "POST",
  "path": "/convert/html-to-pdf",
  "statusCode": 200,
  "processingTimeMs": 842.51,
  "htmlSizeBytes": 19428,
  "fileName": "trade-ticket.pdf",
  "externalResourceCount": 4,
  "failedResourceCount": 0,
  "finalStatus": "SUCCESS"
}
```

## Troubleshooting

### El health check falla en Render

- Verifica que Render esté exponiendo el puerto `10000` o que la variable `PORT` se esté inyectando correctamente.
- Revisa los logs de startup para confirmar el evento `browser_started`.

### El PDF sale sin imágenes o estilos

- Verifica que las URLs externas sean accesibles desde Internet y no requieran autenticación.
- Si `STRICT_EXTERNAL_RESOURCES=true`, revisa si la respuesta fue `422 EXTERNAL_RESOURCE_LOAD_FAILED`.
- Si `STRICT_EXTERNAL_RESOURCES=false`, revisa los headers `X-External-Resources-Status` y `X-Failed-Resource-Count`.
- Confirma que el HTML entregue URLs absolutas `https://`.

### El proceso tarda demasiado

- Aumenta `RENDER_TIMEOUT_MS`.
- Reduce recursos externos pesados o HTML excesivamente grande.

### El servicio responde 500

- Busca el `traceId` en los logs.
- Revisa si el error fue `BROWSER_RENDER_ERROR` o `UNEXPECTED_SERVER_ERROR`.

## Notas operativas

- No se usa autenticación en esta primera versión.
- No se guardan archivos permanentemente en disco.
- El nombre del PDF se sanea para evitar caracteres problemáticos.
- El servicio devuelve `X-Trace-Id` en la respuesta para correlacionar con logs.
