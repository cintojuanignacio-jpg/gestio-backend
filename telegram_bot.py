"""
╔══════════════════════════════════════════════════════════════╗
║  CAFETERÍA MADRID · Bot de Telegram  (versión Google Sheets) ║
║                                                              ║
║  Comandos:                                                   ║
║  /start      → bienvenida                                    ║
║  /resumen    → gastos del mes actual                         ║
║  /iva        → IVA soportado del trimestre                   ║
║  /pendientes → facturas por revisar                          ║
║  + foto/PDF  → procesa como factura automáticamente          ║
╚══════════════════════════════════════════════════════════════╝
"""

import os, logging, tempfile
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters
from dotenv import load_dotenv
from procesador_facturas import procesar_factura
from sheets_manager import obtener_resumen_mes, obtener_iva_trimestre, obtener_pendientes

load_dotenv()

logging.basicConfig(format="%(asctime)s · %(levelname)s · %(message)s", level=logging.INFO)

CHAT_ID = int(os.environ["TELEGRAM_ALLOWED_CHAT_ID"])


def solo_propietario(func):
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if update.effective_chat.id != CHAT_ID:
            await update.message.reply_text("Acceso no autorizado.")
            return
        return await func(update, ctx)
    return wrapper


@solo_propietario
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "¡Hola! Soy el asistente de tu cafetería\n\n"
        "Envíame:\n"
        "📷 Una *foto* de un ticket o factura\n"
        "📄 Un *PDF* de factura\n\n"
        "Comandos:\n"
        "/resumen → gastos del mes\n"
        "/iva → IVA del trimestre\n"
        "/pendientes → facturas por revisar\n"
        "/ayuda → esta lista",
        parse_mode="Markdown"
    )


@solo_propietario
async def cmd_ayuda(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await cmd_start(update, ctx)


@solo_propietario
async def cmd_resumen(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Consultando Google Sheets...")
    try:
        d = obtener_resumen_mes()
        lineas = [f"📊 *Gastos {d['mes']}*\n"]
        for cat, importe in d["por_categoria"].items():
            lineas.append(f"  {cat.replace('_',' ').title()}: {importe:.2f}€")
        lineas += [
            "\n─────────────────",
            f"Base imponible: {d['total_base']:.2f}€",
            f"IVA soportado:  {d['total_iva']:.2f}€",
            f"*Total gastos:  {d['total_gasto']:.2f}€*",
            f"Nº facturas: {d['num_facturas']}",
        ]
        await update.message.reply_text("\n".join(lineas), parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"Error al consultar: {e}")


@solo_propietario
async def cmd_iva(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Calculando IVA del trimestre...")
    try:
        d = obtener_iva_trimestre()
        meses = {1:"Ene–Mar", 2:"Abr–Jun", 3:"Jul–Sep", 4:"Oct–Dic"}
        periodo = meses.get(d["trimestre"], f"T{d['trimestre']}")
        msg = (
            f"📋 *IVA Trimestre {d['trimestre']} ({periodo} {d['ejercicio']})*\n\n"
            f"IVA soportado 21%: {d['soportado_21']:.2f}€\n"
            f"IVA soportado 10%: {d['soportado_10']:.2f}€\n"
            f"IVA soportado  4%: {d['soportado_4']:.2f}€\n"
            f"─────────────────────────\n"
            f"*Total soportado: {d['total_soportado']:.2f}€*\n\n"
            f"_Consulta el IVA repercutido de tus ventas Square en el dashboard._"
        )
        await update.message.reply_text(msg, parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"Error al calcular IVA: {e}")


@solo_propietario
async def cmd_pendientes(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        pendientes = obtener_pendientes(limite=5)
        if not pendientes:
            await update.message.reply_text("✅ No hay facturas pendientes de revisión.")
            return
        lineas = [f"⚠️ *{len(pendientes)} facturas pendientes:*\n"]
        for f in pendientes:
            conf = f.get("Confianza IA (%)", "?")
            lineas.append(
                f"• {f.get('Proveedor','?')} · {f.get('Total (€)','?')}€ "
                f"({f.get('Fecha factura','?')}) · confianza {conf}%"
            )
        lineas.append("\nRevísalas en tu Google Sheet.")
        await update.message.reply_text("\n".join(lineas), parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")


@solo_propietario
async def recibir_foto(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Foto recibida 📷 Procesando con IA...")
    try:
        foto = update.message.photo[-1]
        archivo_tg = await ctx.bot.get_file(foto.file_id)
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
            await archivo_tg.download_to_drive(tmp.name)
            tmp_path = tmp.name

        resultado = procesar_factura(tmp_path, origen="telegram")
        await update.message.reply_text(
            resultado["mensaje"] if resultado["exito"]
            else f"No pude procesar la imagen.\n{resultado['error']}"
        )
        os.unlink(tmp_path)
    except Exception as e:
        await update.message.reply_text(f"Error inesperado: {e}")


@solo_propietario
async def recibir_documento(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document
    if doc.mime_type != "application/pdf":
        await update.message.reply_text("Solo acepto PDFs o fotos.")
        return

    await update.message.reply_text("PDF recibido 📄 Procesando...")
    try:
        archivo_tg = await ctx.bot.get_file(doc.file_id)
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            await archivo_tg.download_to_drive(tmp.name)
            tmp_path = tmp.name

        resultado = procesar_factura(tmp_path, origen="telegram")
        await update.message.reply_text(
            resultado["mensaje"] if resultado["exito"]
            else f"No pude procesar el PDF.\n{resultado['error']}"
        )
        os.unlink(tmp_path)
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")


@solo_propietario
async def mensaje_texto(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Envíame una *foto* o *PDF* de una factura para procesarla.\n"
        "Usa /ayuda para ver los comandos.",
        parse_mode="Markdown"
    )


def main():
    app = ApplicationBuilder().token(os.environ["TELEGRAM_BOT_TOKEN"]).build()
    app.add_handler(CommandHandler("start",      cmd_start))
    app.add_handler(CommandHandler("ayuda",      cmd_ayuda))
    app.add_handler(CommandHandler("resumen",    cmd_resumen))
    app.add_handler(CommandHandler("iva",        cmd_iva))
    app.add_handler(CommandHandler("pendientes", cmd_pendientes))
    app.add_handler(MessageHandler(filters.PHOTO, recibir_foto))
    app.add_handler(MessageHandler(filters.Document.MimeType("application/pdf"), recibir_documento))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, mensaje_texto))
    logging.info("Bot de Telegram arrancado. Esperando mensajes...")
    app.run_polling()


if __name__ == "__main__":
    main()
