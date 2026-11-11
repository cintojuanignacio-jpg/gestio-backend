"""
GESTIO · Bot de Telegram Multi-Cliente
Identifica cada cliente por su Chat ID en Supabase
"""

import base64
import json
import logging
import os
import re
import tempfile
from datetime import date, datetime
from pathlib import Path

import anthropic
import httpx
from telegram import Update
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    ContextTypes, filters
)

logging.basicConfig(
    format="%(asctime)s · %(levelname)s · %(message)s",
    level=logging.INFO
)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
ANTHROPIC_KEY  = os.environ.get("ANTHROPIC_API_KEY", "")
SUPABASE_URL   = os.environ.get("SUPABASE_URL", "https://ulralnthmtrhlqgzlbaj.supabase.co").rstrip("/")
SUPABASE_KEY   = os.environ.get("SUPABASE_SERVICE_KEY", "")
CLAUDE_MODEL   = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-5")

HTTP_TIMEOUT  = 15.0
MAX_UPLOAD_MB = 10
TIPOS_PERMITIDOS = {
    ".pdf":  "application/pdf",
    ".jpg":  "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png":  "image/png",
}
CATEGORIAS_VALIDAS = {
    "materia_prima", "suministros", "servicios",
    "alquiler", "personal", "mantenimiento", "otros",
}


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


# ── Supabase ───────────────────────────────────────────────────────────────────
def sb_headers():
    return {
        "apikey":        SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type":  "application/json",
        "Prefer":        "return=representation"
    }


def buscar_cliente_por_chat_id(chat_id: str):
    """Busca el cliente en Supabase por su Telegram Chat ID."""
    try:
        r = httpx.get(
            f"{SUPABASE_URL}/rest/v1/clientes",
            params={"telegram_chat_id": f"eq.{chat_id}", "activo": "eq.true"},
            headers=sb_headers(),
            timeout=HTTP_TIMEOUT,
        )
        if r.status_code != 200:
            # No se loguea el cuerpo de la respuesta: puede contener datos personales
            logging.error(f"Supabase devolvio {r.status_code} al buscar cliente")
            return None
        data = r.json()
        return data[0] if data else None
    except Exception:
        logging.exception("Error buscando cliente")
        return None


def log_auditoria(user_id, nombre_archivo, accion, datos=None, motivo=None):
    """Registra en logs_ia."""
    try:
        row = {
            "user_id":        user_id,
            "nombre_archivo": nombre_archivo,
            "canal":          "telegram",
            "accion":         accion,
            "proveedor":      datos.get("proveedor") if datos else None,
            "fecha_factura":  fecha_valida(datos.get("fecha")) if datos else None,
            "total":          num(datos.get("total")) if datos else None,
            "confianza_ia":   num(datos.get("confianza_ia")) if datos else None,
            "resultado_ia":   json.dumps(datos) if datos else None,
            "motivo":         motivo
        }
        r = httpx.post(
            f"{SUPABASE_URL}/rest/v1/logs_ia",
            json=row,
            headers=sb_headers(),
            timeout=HTTP_TIMEOUT,
        )
        r.raise_for_status()
    except Exception:
        logging.exception("Error en log de auditoria")


# ── Claude ─────────────────────────────────────────────────────────────────────
_claude = None


def get_claude():
    global _claude
    if _claude is None:
        _claude = anthropic.Anthropic(api_key=ANTHROPIC_KEY, timeout=60.0)
    return _claude


PROMPT_FACTURA = """Analiza esta factura y devuelve SOLO un JSON:
{
  "fecha": "YYYY-MM-DD",
  "proveedor": "nombre",
  "nif_proveedor": "NIF",
  "numero_factura": "numero",
  "descripcion": "descripcion breve",
  "categoria": "materia_prima|suministros|servicios|alquiler|personal|mantenimiento|otros",
  "base_imponible": 0.00,
  "iva_pct": 21,
  "cuota_iva": 0.00,
  "total": 0.00,
  "confianza_ia": 95
}
Si no encuentras un campo pon null. Solo JSON, sin texto adicional."""


def analizar_factura(archivo_path: str) -> dict:
    """Envía la factura a Claude y devuelve los datos estructurados."""
    ext = Path(archivo_path).suffix.lower()
    media_type = TIPOS_PERMITIDOS[ext]
    b64 = base64.b64encode(Path(archivo_path).read_bytes()).decode()

    tipo_bloque = "document" if ext == ".pdf" else "image"
    block = {"type": tipo_bloque, "source": {"type": "base64", "media_type": media_type, "data": b64}}

    resp = get_claude().messages.create(
        model=CLAUDE_MODEL,
        max_tokens=1000,
        messages=[{"role": "user", "content": [block, {"type": "text", "text": PROMPT_FACTURA}]}]
    )
    return parsear_json(resp.content[0].text)


def guardar_factura(user_id: str, datos: dict) -> tuple[bool, bool]:
    """Guarda la factura en Supabase. Devuelve (guardado, duplicado)."""
    try:
        fecha = fecha_valida(datos.get("fecha"))
        fecha_ok = fecha is not None
        if not fecha_ok:
            fecha = date.today().isoformat()
            datos["fecha"] = fecha

        proveedor = datos.get("proveedor")
        numero    = datos.get("numero_factura")
        total     = num(datos.get("total"))

        # Duplicado: con numero de factura -> proveedor + numero; sin el -> proveedor + fecha + total
        if proveedor:
            params = {
                "select":    "id",
                "limit":     "1",
                "user_id":   f"eq.{user_id}",
                "eliminada": "eq.false",
                "proveedor": f"eq.{proveedor}",
            }
            if numero:
                params["numero_factura"] = f"eq.{numero}"
            else:
                params["fecha"] = f"eq.{fecha}"
                params["total"] = f"eq.{total}"
            r = httpx.get(f"{SUPABASE_URL}/rest/v1/facturas", params=params,
                          headers=sb_headers(), timeout=HTTP_TIMEOUT)
            r.raise_for_status()  # un error de Supabase ya NO se confunde con "duplicado"
            if r.json():
                return False, True

        confianza = num(datos.get("confianza_ia"))
        categoria = datos.get("categoria")
        row = {
            "user_id":        user_id,
            "fecha":          fecha,
            "proveedor":      proveedor,
            "nif_proveedor":  datos.get("nif_proveedor"),
            "numero_factura": numero,
            "descripcion":    datos.get("descripcion"),
            "categoria":      categoria if categoria in CATEGORIAS_VALIDAS else "otros",
            "base_imponible": num(datos.get("base_imponible")),
            "iva_pct":        num(datos.get("iva_pct"), 21.0),
            "cuota_iva":      num(datos.get("cuota_iva")),
            "total":          total,
            "confianza_ia":   confianza,
            "estado":         "verificada" if (confianza >= 85 and fecha_ok) else "pendiente",
            "canal":          "telegram",
            "eliminada":      False
        }
        r = httpx.post(f"{SUPABASE_URL}/rest/v1/facturas", json=row,
                       headers=sb_headers(), timeout=HTTP_TIMEOUT)
        r.raise_for_status()  # antes se decia "guardada" aunque Supabase rechazara el insert
        return True, False
    except Exception:
        logging.exception("Error guardando factura")
        return False, False


# ── HANDLERS ───────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    cliente = buscar_cliente_por_chat_id(chat_id)

    if cliente:
        await update.message.reply_text(
            f"Hola, {cliente['nombre_negocio']}! \n\n"
            f"Estoy listo para procesar tus facturas.\n"
            f"Enviame una foto o PDF y lo registro automaticamente."
        )
    else:
        await update.message.reply_text(
            f"Hola! Soy el asistente de Gestio.\n\n"
            f"Tu Chat ID es: `{chat_id}`\n\n"
            f"Copia este numero y pegalo en tu perfil de Gestio "
            f"(Perfil > Configuracion > Chat ID de Telegram) para activar este bot.",
            parse_mode="Markdown"
        )


async def cmd_ayuda(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Comandos disponibles:\n\n"
        "/start - Ver tu Chat ID o estado de conexion\n"
        "/resumen - Gastos del mes actual\n"
        "/ayuda - Ver esta ayuda\n\n"
        "Envia una foto o PDF de factura para procesarla automaticamente."
    )


async def cmd_resumen(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    cliente = buscar_cliente_por_chat_id(chat_id)

    if not cliente:
        await update.message.reply_text(
            "No encontre tu cuenta. Usa /start para obtener tu Chat ID "
            "y configuralo en tu perfil de Gestio."
        )
        return

    try:
        hoy = date.today()
        inicio = hoy.replace(day=1)
        fin = date(hoy.year + (hoy.month == 12), hoy.month % 12 + 1, 1)
        r = httpx.get(
            f"{SUPABASE_URL}/rest/v1/facturas",
            # lista de tuplas: "fecha" aparece dos veces (rango del mes)
            params=[
                ("user_id",   f"eq.{cliente['user_id']}"),
                ("eliminada", "eq.false"),
                ("fecha",     f"gte.{inicio.isoformat()}"),
                ("fecha",     f"lt.{fin.isoformat()}"),
                ("select",    "total,base_imponible,cuota_iva,estado"),
            ],
            headers=sb_headers(),
            timeout=HTTP_TIMEOUT,
        )
        r.raise_for_status()
        facturas = r.json()
        total    = sum(num(f.get("total")) for f in facturas)
        base     = sum(num(f.get("base_imponible")) for f in facturas)
        iva      = sum(num(f.get("cuota_iva")) for f in facturas)
        pend     = sum(1 for f in facturas if f.get("estado") == "pendiente")

        await update.message.reply_text(
            f"Resumen de {cliente['nombre_negocio']} ({hoy.strftime('%m/%Y')}):\n\n"
            f"Total gastos: {total:,.2f} EUR\n"
            f"Base imponible: {base:,.2f} EUR\n"
            f"IVA soportado: {iva:,.2f} EUR\n"
            f"Facturas del mes: {len(facturas)}\n"
            f"Pendientes de revision: {pend}"
        )
    except Exception:
        logging.exception("Error al obtener el resumen")
        await update.message.reply_text("No pude obtener el resumen. Intentalo de nuevo en un momento.")


async def procesar_archivo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    cliente = buscar_cliente_por_chat_id(chat_id)

    if not cliente:
        await update.message.reply_text(
            "No encontre tu cuenta vinculada.\n\n"
            "Usa /start para obtener tu Chat ID y configuralo en "
            "Gestio (Perfil > Configuracion > Chat ID de Telegram)."
        )
        return

    # Validar formato y tamaño ANTES de descargar nada
    if update.message.photo:
        tg_file = update.message.photo[-1]
        nombre_archivo = "foto.jpg"
        ext = ".jpg"
    elif update.message.document:
        tg_file = update.message.document
        nombre_archivo = tg_file.file_name or "factura.pdf"
        ext = Path(nombre_archivo).suffix.lower() or ".pdf"
    else:
        await update.message.reply_text("Formato no soportado. Envia una foto o PDF.")
        return

    if ext not in TIPOS_PERMITIDOS:
        await update.message.reply_text("Formato no soportado. Envia una foto, PDF, JPG o PNG.")
        return
    if (tg_file.file_size or 0) > MAX_UPLOAD_MB * 1024 * 1024:
        await update.message.reply_text(f"El archivo supera el maximo de {MAX_UPLOAD_MB} MB.")
        return

    msg = await update.message.reply_text("Procesando factura...")

    tmp_path = None
    try:
        file = await tg_file.get_file()
        with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as tmp:
            tmp_path = tmp.name

        await file.download_to_drive(tmp_path)

        # Analyze
        datos = analizar_factura(tmp_path)
        guardado, duplicado = guardar_factura(cliente["user_id"], datos)

        # Log
        log_auditoria(
            cliente["user_id"],
            nombre_archivo,
            "duplicada" if duplicado else ("procesada" if guardado else "error"),
            datos,
            motivo="Duplicada" if duplicado else None
        )

        if duplicado:
            await msg.edit_text(
                f"Factura duplicada\n"
                f"{datos.get('proveedor') or '?'} - {datos.get('fecha') or ''} - {datos.get('total') or 0} EUR\n"
                f"Ya existe en tu base de datos."
            )
        elif guardado:
            confianza = num(datos.get("confianza_ia"))
            estado = "Verificada" if confianza >= 85 else "Pendiente de revision"
            await msg.edit_text(
                f"Factura guardada\n\n"
                f"Proveedor: {datos.get('proveedor') or '?'}\n"
                f"Fecha: {datos.get('fecha') or '?'}\n"
                f"Total: {datos.get('total') or 0} EUR\n"
                f"IVA: {datos.get('iva_pct') or 21}%\n"
                f"Categoria: {datos.get('categoria') or 'otros'}\n"
                f"Confianza IA: {int(confianza)}%\n"
                f"Estado: {estado}"
            )
        else:
            await msg.edit_text("Error al guardar la factura. Intentalo de nuevo.")

    except json.JSONDecodeError:
        log_auditoria(cliente["user_id"], nombre_archivo, "error",
                      motivo="Claude no devolvio JSON valido")
        await msg.edit_text("No pude leer la factura. Asegurate de que la imagen sea clara.")
    except Exception:
        logging.exception("Error procesando archivo")
        await msg.edit_text("Error al procesar la factura. Intentalo de nuevo en un momento.")
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.remove(tmp_path)


async def log_update(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # Solo el id: el update completo contiene nombres, chat ids y mensajes de clientes
    logging.debug(f"Update sin manejar: {update.update_id}")


def main():
    if not TELEGRAM_TOKEN:
        raise SystemExit("Falta la variable de entorno TELEGRAM_BOT_TOKEN")
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("ayuda",  cmd_ayuda))
    app.add_handler(CommandHandler("resumen", cmd_resumen))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.ALL, procesar_archivo))
    app.add_handler(MessageHandler(filters.ALL, log_update))
    logging.info("Bot Gestio iniciado - multi-cliente")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
