"""
GESTIO · Bot de Telegram Multi-Cliente
Identifica cada cliente por su Chat ID en Supabase
"""

import os
import logging
import tempfile
import shutil
import json
import base64
import httpx
import anthropic
from pathlib import Path
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
SUPABASE_URL   = os.environ.get("SUPABASE_URL", "https://ulralnthmtrhlqgzlbaj.supabase.co")
SUPABASE_KEY   = os.environ.get("SUPABASE_SERVICE_KEY", "")

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
        logging.info(f"Buscando cliente con chat_id: {chat_id}")
        r = httpx.get(
            f"{SUPABASE_URL}/rest/v1/clientes",
            params={"telegram_chat_id": f"eq.{chat_id}", "activo": "eq.true"},
            headers=sb_headers()
        )
        logging.info(f"Supabase status: {r.status_code}")
        logging.info(f"Supabase response: {r.text[:200]}")
        data = r.json()
        return data[0] if data else None
    except Exception as e:
        logging.error(f"Error buscando cliente: {e}")
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
            "fecha_factura":  datos.get("fecha") if datos else None,
            "total":          float(datos.get("total", 0)) if datos else None,
            "confianza_ia":   float(datos.get("confianza_ia", 0)) if datos else None,
            "resultado_ia":   json.dumps(datos) if datos else None,
            "motivo":         motivo
        }
        httpx.post(
            f"{SUPABASE_URL}/rest/v1/logs_ia",
            json=row,
            headers=sb_headers()
        )
    except Exception as e:
        logging.error(f"Error en log: {e}")

def analizar_factura(archivo_path: str) -> dict:
    """Envía la factura a Claude y devuelve los datos estructurados."""
    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
    ext = Path(archivo_path).suffix.lower()
    with open(archivo_path, "rb") as f:
        raw = f.read()
    b64 = base64.b64encode(raw).decode()

    if ext == ".pdf":
        block = {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": b64}}
    else:
        mt = "image/jpeg" if ext in [".jpg", ".jpeg"] else "image/png"
        block = {"type": "image", "source": {"type": "base64", "media_type": mt, "data": b64}}

    prompt = """Analiza esta factura y devuelve SOLO un JSON:
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
Solo JSON, sin texto adicional."""

    resp = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=1000,
        messages=[{"role": "user", "content": [block, {"type": "text", "text": prompt}]}]
    )
    text = resp.content[0].text.strip().replace("```json", "").replace("```", "").strip()
    return json.loads(text)

def guardar_factura(user_id: str, datos: dict) -> tuple[bool, bool]:
    """Guarda la factura en Supabase. Devuelve (guardado, duplicado)."""
    try:
        # Check duplicate
        r = httpx.get(
            f"{SUPABASE_URL}/rest/v1/facturas",
            params={
                "user_id":    f"eq.{user_id}",
                "proveedor":  f"eq.{datos.get('proveedor','')}",
                "fecha":      f"eq.{datos.get('fecha','')}",
                "total":      f"eq.{datos.get('total',0)}",
                "eliminada":  "eq.false"
            },
            headers=sb_headers()
        )
        if r.json():
            return False, True  # duplicado

        confianza = float(datos.get("confianza_ia", 0))
        row = {
            "user_id":        user_id,
            "fecha":          datos.get("fecha"),
            "proveedor":      datos.get("proveedor"),
            "nif_proveedor":  datos.get("nif_proveedor"),
            "numero_factura": datos.get("numero_factura"),
            "descripcion":    datos.get("descripcion"),
            "categoria":      datos.get("categoria", "otros"),
            "base_imponible": float(datos.get("base_imponible") or 0),
            "iva_pct":        float(datos.get("iva_pct") or 21),
            "cuota_iva":      float(datos.get("cuota_iva") or 0),
            "total":          float(datos.get("total") or 0),
            "confianza_ia":   confianza,
            "estado":         "verificada" if confianza >= 85 else "pendiente",
            "canal":          "telegram",
            "eliminada":      False
        }
        httpx.post(f"{SUPABASE_URL}/rest/v1/facturas", json=row, headers=sb_headers())
        return True, False
    except Exception as e:
        logging.error(f"Error guardando factura: {e}")
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
        from datetime import date
        hoy = date.today()
        r = httpx.get(
            f"{SUPABASE_URL}/rest/v1/facturas",
            params={
                "user_id":  f"eq.{cliente['user_id']}",
                "eliminada": "eq.false",
                "select":   "total,base_imponible,cuota_iva,estado"
            },
            headers=sb_headers()
        )
        facturas = r.json()
        total    = sum(float(f.get("total", 0)) for f in facturas)
        base     = sum(float(f.get("base_imponible", 0)) for f in facturas)
        iva      = sum(float(f.get("cuota_iva", 0)) for f in facturas)
        pend     = sum(1 for f in facturas if f.get("estado") == "pendiente")

        await update.message.reply_text(
            f"Resumen de {cliente['nombre_negocio']}:\n\n"
            f"Total gastos: {total:,.2f} EUR\n"
            f"Base imponible: {base:,.2f} EUR\n"
            f"IVA soportado: {iva:,.2f} EUR\n"
            f"Facturas totales: {len(facturas)}\n"
            f"Pendientes de revision: {pend}"
        )
    except Exception as e:
        await update.message.reply_text(f"Error al obtener el resumen: {str(e)}")

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

    msg = await update.message.reply_text("Procesando factura...")

    tmp_path = None
    try:
        # Download file
        if update.message.photo:
            file = await update.message.photo[-1].get_file()
            ext = ".jpg"
        elif update.message.document:
            file = await update.message.document.get_file()
            fname = update.message.document.file_name or "factura.pdf"
            ext = Path(fname).suffix.lower() or ".pdf"
        else:
            await msg.edit_text("Formato no soportado. Envia una foto o PDF.")
            return

        with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as tmp:
            tmp_path = tmp.name

        await file.download_to_drive(tmp_path)
        nombre_archivo = Path(tmp_path).name

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
                f"{datos.get('proveedor','?')} - {datos.get('fecha','')} - {datos.get('total',0)} EUR\n"
                f"Ya existe en tu base de datos."
            )
        elif guardado:
            confianza = float(datos.get("confianza_ia", 0))
            estado = "Verificada" if confianza >= 85 else "Pendiente de revision"
            await msg.edit_text(
                f"Factura guardada\n\n"
                f"Proveedor: {datos.get('proveedor','?')}\n"
                f"Fecha: {datos.get('fecha','?')}\n"
                f"Total: {datos.get('total',0)} EUR\n"
                f"IVA: {datos.get('iva_pct',21)}%\n"
                f"Categoria: {datos.get('categoria','otros')}\n"
                f"Confianza IA: {int(confianza)}%\n"
                f"Estado: {estado}"
            )
        else:
            await msg.edit_text("Error al guardar la factura. Intentalo de nuevo.")

    except json.JSONDecodeError:
        log_auditoria(cliente["user_id"] if cliente else None, "archivo", "error",
                      motivo="Claude no devolvio JSON valido")
        await msg.edit_text("No pude leer la factura. Asegurate de que la imagen sea clara.")
    except Exception as e:
        logging.error(f"Error procesando archivo: {e}")
        await msg.edit_text(f"Error al procesar: {str(e)}")
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.remove(tmp_path)

async def log_update(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    logging.info(f"UPDATE RECIBIDO: {update}")

def main():
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
