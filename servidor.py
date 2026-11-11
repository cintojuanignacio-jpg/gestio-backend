"""
GESTIO · Backend v2.2
FastAPI + Claude (lectura de facturas) + Supabase (almacenamiento)

Seguridad:
  - Todos los endpoints (salvo /health) exigen un token de Supabase Auth
    en la cabecera  Authorization: Bearer <access_token>
  - El user_id SIEMPRE sale del token, nunca de lo que mande el cliente
  - /admin/* solo para los emails listados en ADMIN_EMAILS
"""
import base64
import json
import logging
import os
import re
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path

import anthropic
import httpx
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

logging.basicConfig(level=logging.INFO, format="%(asctime)s · %(levelname)s · %(message)s")
log = logging.getLogger("gestio")

# ── Configuracion (variables de entorno) ───────────────────────────────────────
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
SUPABASE_URL      = os.environ.get("SUPABASE_URL", "https://ulralnthmtrhlqgzlbaj.supabase.co").rstrip("/")
SUPABASE_KEY      = os.environ.get("SUPABASE_SERVICE_KEY", "")
# Clave publica (anon) de Supabase, solo se usa para validar tokens de usuario.
SUPABASE_ANON_KEY = os.environ.get("SUPABASE_ANON_KEY", "") or SUPABASE_KEY
CLAUDE_MODEL      = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-5")

# Emails con acceso a /admin/*  ->  ADMIN_EMAILS="tu@email.com,otro@email.com"
ADMIN_EMAILS = {e.strip().lower() for e in os.environ.get("ADMIN_EMAILS", "").split(",") if e.strip()}

# Dominios permitidos por CORS  ->  ALLOWED_ORIGINS="https://tuweb.com,https://www.tuweb.com"
ALLOWED_ORIGINS = [o.strip() for o in os.environ.get("ALLOWED_ORIGINS", "").split(",") if o.strip()]

MAX_UPLOAD_MB = 10
HTTP_TIMEOUT  = 15.0
TIPOS_PERMITIDOS = {
    ".pdf":  "application/pdf",
    ".jpg":  "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png":  "image/png",
}
ORIGENES_VALIDOS = {"web", "telegram"}
CATEGORIAS_VALIDAS = {
    "materia_prima", "suministros", "servicios",
    "alquiler", "personal", "mantenimiento", "otros",
}

if not ALLOWED_ORIGINS:
    log.warning("ALLOWED_ORIGINS no esta definida: CORS abierto a cualquier origen. Definela en produccion.")
if not ADMIN_EMAILS:
    log.warning("ADMIN_EMAILS no esta definida: nadie podra usar /admin/*.")
for _nombre, _valor in [("ANTHROPIC_API_KEY", ANTHROPIC_API_KEY),
                        ("SUPABASE_SERVICE_KEY", SUPABASE_KEY),
                        ("SUPABASE_ANON_KEY", os.environ.get("SUPABASE_ANON_KEY", ""))]:
    if not _valor:
        log.warning("Falta la variable de entorno %s", _nombre)

app = FastAPI(title="Gestio Backend v2")

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS or ["*"],
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)


# ── Utilidades ─────────────────────────────────────────────────────────────────
def num(valor, defecto: float = 0.0) -> float:
    """float() tolerante: None, '', 'abc' -> defecto; '12,50' -> 12.5"""
    try:
        if isinstance(valor, str):
            valor = valor.replace(",", ".").strip()
        return float(valor)
    except (TypeError, ValueError):
        return defecto


def fecha_valida(valor):
    """Devuelve 'YYYY-MM-DD' si es una fecha real, si no None."""
    try:
        return datetime.strptime(str(valor), "%Y-%m-%d").date().isoformat()
    except ValueError:
        return None


def respuesta_error(status: int, mensaje: str) -> JSONResponse:
    """Mantiene la forma de respuesta que ya espera el frontend."""
    return JSONResponse(
        status_code=status,
        content={"exito": False, "duplicado": False, "mensaje": mensaje, "guardado": False},
    )


# ── Supabase helpers ───────────────────────────────────────────────────────────
def sb_headers():
    return {
        "apikey":        SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type":  "application/json",
        "Prefer":        "return=representation",
    }


def sb_insert(table: str, data: dict):
    r = httpx.post(f"{SUPABASE_URL}/rest/v1/{table}", json=data,
                   headers=sb_headers(), timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    return r.json()


def sb_get(table: str, params: dict):
    # params como dict -> httpx codifica bien los valores (nada de f-strings en la URL)
    r = httpx.get(f"{SUPABASE_URL}/rest/v1/{table}", params=params,
                  headers=sb_headers(), timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    return r.json()


def sb_patch(table: str, params: dict, data: dict):
    r = httpx.patch(f"{SUPABASE_URL}/rest/v1/{table}", params=params, json=data,
                    headers=sb_headers(), timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    return r.json()


def log_auditoria(user_id, nombre_archivo, canal, accion, datos=None, motivo=None):
    try:
        sb_insert("logs_ia", {
            "user_id":        user_id or None,
            "nombre_archivo": nombre_archivo,
            "canal":          canal,
            "accion":         accion,
            "proveedor":      datos.get("proveedor") if datos else None,
            "fecha_factura":  fecha_valida(datos.get("fecha")) if datos else None,
            "total":          num(datos.get("total")) if datos else None,
            "confianza_ia":   num(datos.get("confianza_ia")) if datos else None,
            "resultado_ia":   json.dumps(datos) if datos else None,
            "motivo":         motivo,
        })
    except Exception:
        log.exception("Error guardando log de auditoria")


# ── Autenticacion ──────────────────────────────────────────────────────────────
def usuario_actual(authorization: str = Header(default="")) -> dict:
    """Valida el token de Supabase Auth y devuelve {'id', 'email'}."""
    if not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Falta el token de autenticacion")
    token = authorization[7:].strip()
    if not token:
        raise HTTPException(status_code=401, detail="Falta el token de autenticacion")
    try:
        r = httpx.get(
            f"{SUPABASE_URL}/auth/v1/user",
            headers={"apikey": SUPABASE_ANON_KEY, "Authorization": f"Bearer {token}"},
            timeout=HTTP_TIMEOUT,
        )
    except httpx.HTTPError:
        log.exception("No se pudo contactar con Supabase Auth")
        raise HTTPException(status_code=503, detail="No se pudo verificar la sesion")
    if r.status_code != 200:
        raise HTTPException(status_code=401, detail="Sesion invalida o expirada")
    u = r.json()
    return {"id": u["id"], "email": (u.get("email") or "").lower()}


def admin_actual(user: dict = Depends(usuario_actual)) -> dict:
    if user["email"] not in ADMIN_EMAILS:
        raise HTTPException(status_code=403, detail="Acceso restringido")
    return user


def solo_propietario(user_id: str, user: dict):
    if user_id != user["id"]:
        raise HTTPException(status_code=403, detail="No autorizado")


# ── Claude ─────────────────────────────────────────────────────────────────────
_claude = None


def get_claude():
    global _claude
    if _claude is None:
        _claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY, timeout=60.0)
    return _claude


def parsear_json(texto: str) -> dict:
    texto = texto.strip()
    try:
        d = json.loads(texto)
    except json.JSONDecodeError:
        m = re.search(r"\{[\s\S]*\}", texto)
        if not m:
            raise
        d = json.loads(m.group())
    if not isinstance(d, dict):
        raise json.JSONDecodeError("La respuesta no es un objeto JSON", texto, 0)
    return d


PROMPT_FACTURA = """Analiza esta factura y devuelve SOLO un JSON con estos campos:
{
  "fecha": "YYYY-MM-DD",
  "proveedor": "nombre del proveedor",
  "nif_proveedor": "NIF o CIF",
  "numero_factura": "numero de factura",
  "descripcion": "descripcion breve",
  "categoria": "una de: materia_prima, suministros, servicios, alquiler, personal, mantenimiento, otros",
  "base_imponible": 0.00,
  "iva_pct": 21,
  "cuota_iva": 0.00,
  "total": 0.00,
  "confianza_ia": 95
}
Si no encuentras un campo pon null. Devuelve SOLO el JSON, sin texto adicional."""


def analizar_factura_con_claude(archivo_path: str) -> dict:
    ext = Path(archivo_path).suffix.lower()
    media_type = TIPOS_PERMITIDOS[ext]
    b64 = base64.b64encode(Path(archivo_path).read_bytes()).decode()

    tipo_bloque = "document" if ext == ".pdf" else "image"
    bloque = {"type": tipo_bloque, "source": {"type": "base64", "media_type": media_type, "data": b64}}

    response = get_claude().messages.create(
        model=CLAUDE_MODEL,
        max_tokens=1000,
        messages=[{"role": "user", "content": [bloque, {"type": "text", "text": PROMPT_FACTURA}]}],
    )
    return parsear_json(response.content[0].text)


# ── Duplicados ─────────────────────────────────────────────────────────────────
def es_duplicada(user_id: str, row: dict) -> bool:
    """Con numero de factura: mismo proveedor + numero. Sin el: proveedor + fecha + total."""
    if not row.get("proveedor"):
        return False  # sin proveedor no se puede comparar con fiabilidad
    params = {
        "select":    "id",
        "limit":     "1",
        "user_id":   f"eq.{user_id}",
        "eliminada": "eq.false",
        "proveedor": f"eq.{row['proveedor']}",
    }
    if row.get("numero_factura"):
        params["numero_factura"] = f"eq.{row['numero_factura']}"
    else:
        params["fecha"] = f"eq.{row['fecha']}"
        params["total"] = f"eq.{row['total']}"
    return bool(sb_get("facturas", params))


# ── Endpoints ──────────────────────────────────────────────────────────────────
@app.get("/health")
def health():
    return {"estado": "ok", "version": "2.2"}


# Nota: los endpoints son `def` (no `async def`) a proposito: FastAPI los ejecuta en un
# hilo aparte, asi una llamada lenta a Claude no bloquea al resto de peticiones.
@app.post("/procesar-factura")
def procesar_factura(
    archivo: UploadFile = File(...),
    origen:  str        = Form(default="web"),
    user:    dict       = Depends(usuario_actual),
):
    user_id = user["id"]
    nombre  = archivo.filename or "factura"
    ext     = Path(nombre).suffix.lower()
    if ext not in TIPOS_PERMITIDOS:
        return respuesta_error(400, "Formato no soportado. Sube un PDF, JPG o PNG.")
    if origen not in ORIGENES_VALIDOS:
        origen = "web"

    max_bytes = MAX_UPLOAD_MB * 1024 * 1024
    tmp_path = None
    try:
        # Guardar en disco leyendo por trozos, cortando si supera el limite
        with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as tmp:
            tmp_path = tmp.name
            leido = 0
            while True:
                trozo = archivo.file.read(1024 * 1024)
                if not trozo:
                    break
                leido += len(trozo)
                if leido > max_bytes:
                    return respuesta_error(413, f"El archivo supera el maximo de {MAX_UPLOAD_MB} MB.")
                tmp.write(trozo)
        if leido == 0:
            return respuesta_error(400, "El archivo esta vacio.")

        datos = analizar_factura_con_claude(tmp_path)

        # Normalizar lo que devuelve la IA (puede venir null o con formato raro)
        confianza = num(datos.get("confianza_ia"))
        fecha     = fecha_valida(datos.get("fecha"))
        fecha_ok  = fecha is not None
        if not fecha_ok:
            fecha = date.today().isoformat()
            datos["fecha"] = fecha
        categoria = datos.get("categoria")
        if categoria not in CATEGORIAS_VALIDAS:
            categoria = "otros"

        row = {
            "user_id":        user_id,
            "fecha":          fecha,
            "proveedor":      datos.get("proveedor"),
            "nif_proveedor":  datos.get("nif_proveedor"),
            "numero_factura": datos.get("numero_factura"),
            "descripcion":    datos.get("descripcion"),
            "categoria":      categoria,
            "base_imponible": num(datos.get("base_imponible")),
            "iva_pct":        num(datos.get("iva_pct"), 21.0),
            "cuota_iva":      num(datos.get("cuota_iva")),
            "total":          num(datos.get("total")),
            "confianza_ia":   confianza,
            "canal":          origen,
            "eliminada":      False,
            # Sin fecha legible la factura siempre queda pendiente de revision
            "estado":         "verificada" if (confianza >= 85 and fecha_ok) else "pendiente",
        }

        guardado = duplicado = False
        try:
            if es_duplicada(user_id, row):
                duplicado = True
                log_auditoria(user_id, nombre, origen, "duplicada", datos,
                              motivo="Ya existia una factura igual")
            else:
                sb_insert("facturas", row)
                guardado = True
                log_auditoria(user_id, nombre, origen, "procesada", datos)
        except Exception as e:
            log.exception("Error guardando factura en Supabase")
            log_auditoria(user_id, nombre, origen, "error", datos, motivo=str(e))

        if duplicado:
            msg = "Ya existe en el sistema."
        elif guardado:
            estado_txt = "Verificada" if row["estado"] == "verificada" else "Pendiente"
            msg = (f"OK: {row['proveedor'] or '?'} | {fecha} | {row['total']} EUR | "
                   f"IVA {row['iva_pct']:g}% | Confianza {int(confianza)}% | {estado_txt}")
        else:
            msg = "Error al guardar la factura. Intentalo de nuevo."

        return {
            "exito":     guardado,
            "duplicado": duplicado,
            "mensaje":   msg,
            "datos":     datos,
            "guardado":  guardado,
        }

    except json.JSONDecodeError as e:
        log.warning("Claude no devolvio JSON valido: %s", e)
        log_auditoria(user_id, nombre, origen, "error", motivo=f"Claude no devolvio JSON valido: {e}")
        return respuesta_error(422, "No se pudo leer la factura. Intentalo con una imagen mas clara.")
    except Exception as e:
        log.exception("Error procesando factura")
        log_auditoria(user_id, nombre, origen, "error", motivo=str(e))
        return respuesta_error(500, "Error interno al procesar la factura. Intentalo de nuevo.")
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.remove(tmp_path)


class ChatIn(BaseModel):
    mensaje: str = Field(min_length=1, max_length=2000)


@app.post("/chat")
def chat(data: ChatIn, user: dict = Depends(usuario_actual)):
    try:
        response = get_claude().messages.create(
            model=CLAUDE_MODEL,
            max_tokens=500,
            system="Eres un asistente de facturacion para comercios espanoles. "
                   "Responde de forma concisa sobre IVA, gastos, facturas y contabilidad.",
            messages=[{"role": "user", "content": data.mensaje}],
        )
        return {"respuesta": response.content[0].text}
    except Exception:
        log.exception("Error en /chat")
        raise HTTPException(status_code=500, detail="No se pudo obtener respuesta. Intentalo de nuevo.")


@app.delete("/facturas/{factura_id}")
def eliminar_factura(factura_id: str, user: dict = Depends(usuario_actual)):
    """Soft delete: solo el propietario puede borrar su factura."""
    try:
        filtro = {"id": f"eq.{factura_id}", "user_id": f"eq.{user['id']}"}
        facturas = sb_get("facturas", {**filtro, "eliminada": "eq.false", "limit": "1"})
        if not facturas:
            raise HTTPException(status_code=404, detail="Factura no encontrada")
        factura = facturas[0]

        sb_patch("facturas", filtro,
                 {"eliminada": True, "eliminada_en": datetime.now(timezone.utc).isoformat()})

        log_auditoria(
            user["id"],
            factura.get("numero_factura") or factura.get("descripcion") or "factura",
            "web",
            "eliminada",
            {
                "proveedor":    factura.get("proveedor"),
                "fecha":        str(factura.get("fecha") or ""),
                "total":        factura.get("total"),
                "confianza_ia": factura.get("confianza_ia"),
            },
            motivo="Eliminada por el usuario",
        )
        return {"exito": True}
    except HTTPException:
        raise
    except Exception:
        log.exception("Error eliminando factura %s", factura_id)
        raise HTTPException(status_code=500, detail="No se pudo eliminar la factura")


# El user_id sigue en la ruta para no romper el frontend, pero se compara con el del token.
@app.get("/facturas/{user_id}")
def get_facturas(user_id: str, user: dict = Depends(usuario_actual)):
    solo_propietario(user_id, user)
    try:
        data = sb_get("facturas", {
            "user_id":   f"eq.{user_id}",
            "eliminada": "eq.false",
            "order":     "fecha.desc",
        })
        return {"facturas": data}
    except Exception:
        log.exception("Error leyendo facturas")
        raise HTTPException(status_code=500, detail="No se pudieron cargar las facturas")


@app.get("/logs/{user_id}")
def get_logs(user_id: str, user: dict = Depends(usuario_actual)):
    solo_propietario(user_id, user)
    try:
        data = sb_get("logs_ia", {
            "user_id": f"eq.{user_id}",
            "order":   "creado_en.desc",
            "limit":   "50",
        })
        return {"logs": data}
    except Exception:
        log.exception("Error leyendo logs")
        raise HTTPException(status_code=500, detail="No se pudieron cargar los logs")


# ── Admin (solo emails en ADMIN_EMAILS) ────────────────────────────────────────
@app.get("/admin/logs")
def get_all_logs(admin: dict = Depends(admin_actual)):
    try:
        return {"logs": sb_get("logs_ia", {"order": "creado_en.desc", "limit": "200"})}
    except Exception:
        log.exception("Error leyendo logs (admin)")
        raise HTTPException(status_code=500, detail="No se pudieron cargar los logs")


@app.get("/admin/tickets")
def get_all_tickets(admin: dict = Depends(admin_actual)):
    try:
        return {"tickets": sb_get("tickets", {"order": "creado_en.desc"})}
    except Exception:
        log.exception("Error leyendo tickets (admin)")
        raise HTTPException(status_code=500, detail="No se pudieron cargar los tickets")


CAMPOS_TICKET_PROTEGIDOS = {"id", "user_id", "creado_en"}


@app.patch("/admin/tickets/{ticket_id}")
def actualizar_ticket(ticket_id: str, data: dict, admin: dict = Depends(admin_actual)):
    cambios = {k: v for k, v in data.items() if k not in CAMPOS_TICKET_PROTEGIDOS}
    if not cambios:
        raise HTTPException(status_code=400, detail="No hay campos para actualizar")
    try:
        filas = sb_patch("tickets", {"id": f"eq.{ticket_id}"}, cambios)
    except Exception:
        log.exception("Error actualizando ticket %s", ticket_id)
        raise HTTPException(status_code=500, detail="No se pudo actualizar el ticket")
    if not filas:
        raise HTTPException(status_code=404, detail="Ticket no encontrado")
    return {"exito": True}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
