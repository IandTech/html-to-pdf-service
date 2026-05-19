# html-to-pdf-service

Servicio REST en Python para convertir HTML, correos `.eml/.msg` y documentos Office a PDF usando FastAPI + Playwright + Chromium. Para Word, Excel y PowerPoint usa LibreOffice headless dentro del contenedor. Esta pensado para flujos de Power Automate que envian uno o varios documentos y necesitan un PDF independiente por cada entrada.

## Caracteristicas

- FastAPI async con endpoints binarios y batch JSON
- Endpoint universal `POST /convert/to-pdf`
- Renderizado HTML real con Chromium headless via Playwright
- Conversion de `.doc/.docx`, `.xls/.xlsx`, `.ppt/.pptx` con LibreOffice
- Soporte de correos `.eml` y `.msg`
- Reutilizacion del motor existente de HTML a PDF
- PDF A4 con fondos impresos y margenes de 10mm
- Browser global en startup y cierre ordenado en shutdown
- Logging estructurado en JSON
- Errores explicitos con `traceId`

## Endpoints

### `GET /`

```json
{
  "service": "html-to-pdf-service",
  "status": "ok",
  "version": "1.0.0"
}
```

### `GET /health`

```json
{
  "status": "healthy",
  "browser": "connected",
  "environment": "render"
}
```

### `POST /convert/html-to-pdf`

Mantiene compatibilidad con el endpoint original.

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

- `HTTP 200`
- `Content-Type: application/pdf`
- body binario PDF
- `Content-Disposition` con el nombre final del PDF

### `POST /convert/to-pdf`

Endpoint universal.

Reglas fijas:

- `1 documento de entrada = 1 PDF de salida`
- multiples documentos de entrada = multiples PDFs independientes en JSON base64
- nunca ZIP
- nunca merge
- nunca PDF combinado

Tipos soportados:

- HTML inline (`html`)
- archivos `.html` y `.htm`
- archivos `.txt`
- archivos `.pdf`
- correos `.eml`
- correos `.msg`
- `.doc` y `.docx`
- `.xls` y `.xlsx`
- `.ppt` y `.pptx`

#### Single item

Request:

```json
{
  "fileName": "main-email.html",
  "html": "<html><body><h1>Main email</h1></body></html>",
  "metadata": {
    "source": "PowerAutomate",
    "ticketId": "20260519_131343_TT"
  }
}
```

Reglas del contenido por item:

- si viene `html`, se procesa como HTML
- si viene `contentBase64`, se procesa como archivo
- si no viene ninguno, devuelve `MISSING_CONTENT`
- si vienen ambos, devuelve `AMBIGUOUS_CONTENT`

Respuesta:

- `HTTP 200`
- `Content-Type: application/pdf`
- body binario PDF
- `Content-Disposition` con nombre final `.pdf`

#### Multiple items

Request:

```json
{
  "items": [
    {
      "fileName": "main-email.html",
      "html": "<html><body><h1>Main email</h1></body></html>",
      "metadata": {
        "source": "PowerAutomate",
        "ticketId": "20260519_131343_TT"
      }
    },
    {
      "fileName": "correo-adjunto.eml",
      "contentBase64": "BASE64_FILE",
      "metadata": {
        "source": "PowerAutomate",
        "ticketId": "20260519_131343_TT"
      }
    }
  ]
}
```

Respuesta:

```json
{
  "success": "partial",
  "traceId": "global-trace-id",
  "results": [
    {
      "index": 0,
      "originalFileName": "main-email.html",
      "outputFileName": "main-email.pdf",
      "status": "success",
      "contentType": "application/pdf",
      "contentBase64": "BASE64_PDF",
      "metadata": {
        "detectedType": "html",
        "source": "PowerAutomate"
      }
    },
    {
      "index": 1,
      "originalFileName": "correo-adjunto.eml",
      "outputFileName": "correo-adjunto.pdf",
      "status": "failed",
      "errorCode": "INVALID_BASE64",
      "message": "El contenido recibido no es un Base64 valido.",
      "technicalDetail": "Base64 decode failed."
    }
  ]
}
```

Significado del campo `success`:

- `true`: todos los documentos fueron exitosos
- `partial`: algunos fueron exitosos y otros fallaron
- `false`: todos fallaron

### `POST /convert/email-to-pdf`

Endpoint legado `multipart/form-data` para convertir un `.eml` o `.msg` individual.

Campos:

- `file`: archivo `.eml` o `.msg`
- `fileName`: opcional
- `metadata`: opcional, JSON serializado como texto

## Variables de entorno

- `RENDER_TIMEOUT_MS`: timeout maximo en milisegundos para render HTML y conversion Office. Default `30000`.
- `LOG_LEVEL`: nivel de logging. Default `INFO`.
- `STRICT_EXTERNAL_RESOURCES`: si es `true`, la API devuelve `422` cuando falla cualquier recurso externo del HTML. Default `false`.
- `LIBREOFFICE_BINARY`: binario de LibreOffice para conversion Office. Default `libreoffice`.

## Ejecucion local con Docker

### Construir imagen

```bash
docker build -t html-to-pdf-service .
```

### Ejecutar contenedor

```bash
docker run --rm -p 10000:10000 \
  -e RENDER_TIMEOUT_MS=30000 \
  -e LOG_LEVEL=INFO \
  -e STRICT_EXTERNAL_RESOURCES=false \
  -e LIBREOFFICE_BINARY=libreoffice \
  html-to-pdf-service
```

### Probar health

```bash
curl http://localhost:10000/health
```

## Ejecucion local sin Docker

```bash
pip install -r requirements.txt
playwright install chromium
uvicorn main:app --host 0.0.0.0 --port 10000
```

Nota:

- Para conversion Office fuera de Docker necesitas tener LibreOffice instalado en el sistema y accesible desde `LIBREOFFICE_BINARY`.

## Ejemplos curl

### Convertir HTML a PDF

```bash
curl -X POST http://localhost:10000/convert/html-to-pdf \
  -H "Content-Type: application/json" \
  --output trade-ticket.pdf \
  -d '{
    "html": "<html><body><h1>Trade Ticket</h1><p>Processed from email.</p></body></html>",
    "fileName": "trade-ticket.pdf",
    "metadata": {
      "source": "PowerAutomate",
      "ticketId": "TT-001"
    }
  }'
```

### Convertir `.eml` con endpoint universal

```bash
curl -X POST http://localhost:10000/convert/to-pdf \
  -H "Content-Type: application/json" \
  --output correo.pdf \
  -d '{
    "fileName": "correo.eml",
    "contentBase64": "BASE64_EML",
    "metadata": {
      "source": "PowerAutomate",
      "ticketId": "TT-EMAIL-001"
    }
  }'
```

### Convertir `.msg` con endpoint universal

```bash
curl -X POST http://localhost:10000/convert/to-pdf \
  -H "Content-Type: application/json" \
  --output correo-msg.pdf \
  -d '{
    "fileName": "correo.msg",
    "contentBase64": "BASE64_MSG",
    "metadata": {
      "source": "PowerAutomate",
      "ticketId": "TT-MSG-001"
    }
  }'
```

### Convertir multiples documentos a PDFs independientes

```bash
curl -X POST http://localhost:10000/convert/to-pdf \
  -H "Content-Type: application/json" \
  -d '{
    "items": [
      {
        "fileName": "main-email.html",
        "html": "<html><body><h1>Main email</h1></body></html>",
        "metadata": { "source": "PowerAutomate" }
      },
      {
        "fileName": "correo-adjunto.msg",
        "contentBase64": "BASE64_MSG",
        "metadata": { "source": "PowerAutomate" }
      },
      {
        "fileName": "documento.xlsx",
        "contentBase64": "BASE64_XLSX",
        "metadata": { "source": "PowerAutomate" }
      }
    ]
  }'
```

### Endpoint legado multipart para `.eml` o `.msg`

```bash
curl -X POST http://localhost:10000/convert/email-to-pdf \
  -F "file=@correo.eml" \
  -F "fileName=correo-renderizado.pdf" \
  -F "metadata={\"source\":\"Postman\",\"ticketId\":\"TT-EMAIL-001\"}" \
  --output correo-renderizado.pdf
```

## Ejemplo Power Automate

### Single item HTML

- Method: `POST`
- URL: `https://<tu-servicio>/convert/to-pdf`
- Headers: `Content-Type: application/json`
- Body:

```json
{
  "fileName": "trade-ticket-@{utcNow('yyyyMMdd-HHmmss')}.html",
  "html": "@{triggerOutputs()?['body/body']}",
  "metadata": {
    "source": "PowerAutomate",
    "ticketId": "@{variables('ticketId')}",
    "emailSubject": "@{triggerOutputs()?['body/subject']}"
  }
}
```

### Single item attachment `.eml` o `.msg`

```json
{
  "fileName": "@{items('Apply_to_each')?['Name']}",
  "contentBase64": "@{body('Get_attachment_content')?['$content']}",
  "metadata": {
    "source": "PowerAutomate",
    "ticketId": "@{variables('ticketId')}",
    "emailSubject": "@{triggerOutputs()?['body/subject']}"
  }
}
```

### Multiple items

Construye un arreglo `items` y envialo al mismo endpoint. Cuando la respuesta sea JSON, cada resultado exitoso trae su propio `contentBase64`.

Nota para Power Automate:

- Para `.eml` y `.msg`, la API tolera un escenario comun donde el adjunto llega doble codificado en Base64 y trata de normalizarlo automaticamente antes del parser.
- Aun asi, el valor ideal para `contentBase64` sigue siendo el Base64 real del archivo, no el MIME crudo en texto.

## Despliegue en Railway

1. Sube el proyecto a GitHub.
2. Conecta el repo en Railway.
3. Railway detecta el `Dockerfile`.
4. Configura variables si quieres ajustar timeout o logs:
   - `RENDER_TIMEOUT_MS=30000`
   - `LOG_LEVEL=INFO`
   - `STRICT_EXTERNAL_RESOURCES=false`
   - `LIBREOFFICE_BINARY=libreoffice`
5. Usa `/health` como health check.

## Errores principales

Todos los errores devuelven JSON estructurado con `traceId`.

### `HTML_EMPTY` - HTTP 400

El campo `html` llego vacio o nulo.

### `INVALID_HTML` - HTTP 400

El payload HTML no pudo procesarse.

### `EMPTY_FILE` - HTTP 400

`contentBase64` llego vacio, nulo o decodifico a cero bytes.

### `INVALID_BASE64` - HTTP 400

El contenido recibido no es un Base64 valido.

### `MISSING_CONTENT` - HTTP 400

El item no incluye `html` ni `contentBase64`.

### `AMBIGUOUS_CONTENT` - HTTP 400

El item incluye `html` y `contentBase64` al mismo tiempo.

### `UNSUPPORTED_FILE_TYPE` - HTTP 400

La extension del documento no es soportada por el endpoint universal.

### `EML_PARSE_FAILED` - HTTP 422

No se pudo parsear correctamente el archivo `.eml`.

### `MSG_PARSE_FAILED` - HTTP 422

No se pudo parsear correctamente el archivo `.msg`.

### `EMAIL_BODY_NOT_FOUND` - HTTP 422

No se encontro cuerpo HTML ni texto plano renderizable dentro del correo.

### `EMAIL_INLINE_IMAGE_WARNING`

No rompe la conversion. Se registra en logs cuando una o mas imagenes inline `cid:` no pudieron resolverse.

### `EMAIL_RENDER_FAILED` - HTTP 500

El correo se parseo correctamente, pero fallo la conversion del HTML reconstruido a PDF.

### `OFFICE_CONVERSION_FAILED` - HTTP 422

LibreOffice no pudo convertir el documento Office a PDF.

### `OFFICE_CONVERSION_UNAVAILABLE` - HTTP 500

LibreOffice no esta disponible en el entorno actual.

### `EXTERNAL_RESOURCE_LOAD_FAILED` - HTTP 422

Chromium detecto recursos externos fallidos y `STRICT_EXTERNAL_RESOURCES=true`.

### `PDF_RENDER_TIMEOUT` - HTTP 408

La generacion del PDF excedio el timeout configurado.

### `UNEXPECTED_SERVER_ERROR` - HTTP 500

Ocurrio un error inesperado. El stack trace completo queda solo en logs internos.

## Logging esperado

Cada request genera logs JSON utiles para debugging, auditoria y soporte:

- `traceId`
- path y metodo
- tamano del HTML o archivo procesado
- `fileName`
- extension detectada
- metadata recibida
- tiempo de parseo por documento
- tiempo de render por documento
- `parserUsed` para `.eml` y `.msg`
- `hasHtmlBody` y `hasPlainTextBody`
- subject extraido
- from extraido
- cantidad de adjuntos internos
- cantidad de imagenes inline detectadas
- cantidad de imagenes CID encontradas
- cantidad de imagenes CID resueltas
- warnings de imagenes inline no resueltas
- cantidad de recursos externos detectados
- cantidad de recursos fallidos
- status final del request

## Troubleshooting

### El PDF sale sin algunas imagenes

- Verifica que las URLs externas sean accesibles desde Internet.
- Si `STRICT_EXTERNAL_RESOURCES=false`, revisa headers `X-External-Resources-Status` y `X-Failed-Resource-Count`.
- Para correos con imagenes inline, la API intenta resolver `cid:` a `data:` cuando es posible.

### Un `.msg` falla pero el texto existe

- Revisa logs por `MSG_PARSE_FAILED`.
- Si el `.msg` no expone HTML, la API intenta usar el cuerpo de texto plano.

### Limitacion conocida para `.msg`

- La resolucion de imagenes inline CID depende de la informacion que `extract-msg` pueda exponer.
- Si no se pueden resolver, la conversion continua y el PDF muestra un warning visible.

### Un Office file no convierte

- Revisa `OFFICE_CONVERSION_FAILED`.
- Busca detalles del filtro o formato en logs.
- Confirma que el archivo no esta corrupto.

### El endpoint batch devolvio `partial`

- Algunos documentos se convirtieron bien y otros fallaron.
- Revisa cada item en `results` y usa solo los que tengan `status: success`.
