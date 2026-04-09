"""
╔══════════════════════════════════════════════════════════════╗
║  GESTIO · Backend Web  — servidor.py                         ║
║                                                              ║
║  Endpoints:                                                  ║
║  POST /procesar-factura   → sube archivo, devuelve datos     ║
║  POST /corregir           → guarda corrección manual         ║
║  POST /chat               → responde preguntas del chatbot   ║
║  GET  /resumen            → KPIs del mes actual              ║
║  GET  /iva                → estado IVA trimestre             ║
║  GET  /pendientes         → facturas pendientes              ║
║  GET  /health             → estado del servidor              ║
╚══════════════════════════════════════════════════════════════╝
"""

import os
import shutil
import tempfile
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, UploadFile, HTTPException, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from dotenv import load_dotenv

load_dotenv()

from procesador_facturas import procesar_factura
from sheets_manager import (
    obtener_resumen_mes, obtener_iva_trimestre, obtener_pendientes
)
from drive_manager import aplicar_correccion, buscar_proveedor_en_memoria

SHEET_ID       = os.environ.get("GOOGLE_SHEET_ID", "")
DRIVE_ROOT_ID  = os.environ.get("GOOGLE_DRIVE_ROOT_ID", "")
CLIENTE_NOMBRE = os.environ.get("CLIENTE_NOMBRE", "Mi_Negocio")

app = FastAPI(title="Gestio API", version="1.0")

# ── CORS: permite que el dashboard web llame al servidor ────
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],          # En producción: solo tu dominio
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── MODELOS ──────────────────────────────────────────────────

class CorreccionRequest(BaseModel):
    fila:              str
    proveedor_nombre:  str
    fecha_factura:     Optional[str] = None
    base_imponible:    Optional[float] = None
    tipo_iva:          Optional[float] = None
    categoria:         Optional[str] = None
    subcategoria:      Optional[str] = None
    notas:             Optional[str] = None


class ChatRequest(BaseModel):
    mensaje: str


# ── HEALTH ───────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"estado": "ok", "version": "1.0", "cliente": CLIENTE_NOMBRE}


# ── PROCESAR FACTURA ─────────────────────────────────────────

@app.post("/procesar-factura")
async def endpoint_procesar_factura(
    archivo:        UploadFile = File(...),
    origen:         str        = Form(default="web"),
    sheet_id:       str        = Form(default=""),
    cliente_nombre: str        = Form(default="")
):
    """
    Recibe un archivo (PDF, JPG, PNG) desde el chat web,
    lo procesa con IA y devuelve los datos extraídos.
    """
    # Validar tipo de archivo
    tipos_permitidos = {"application/pdf", "image/jpeg", "image/png", "image/jpg"}
    if archivo.content_type not in tipos_permitidos:
        raise HTTPException(
            status_code=400,
            detail=f"Tipo de archivo no soportado: {archivo.content_type}. Usa PDF, JPG o PNG."
        )

    # Determinar extensión
    ext_map = {
        "application/pdf": ".pdf",
        "image/jpeg":      ".jpg",
        "image/jpg":       ".jpg",
        "image/png":       ".png",
    }
    extension = ext_map.get(archivo.content_type, ".jpg")

    # Guardar temporalmente
    with tempfile.NamedTemporaryFile(suffix=extension, delete=False) as tmp:
        shutil.copyfileobj(archivo.file, tmp)
        tmp_path = tmp.name

    try:
        # Usar sheet_id del cliente si viene en el request
        import os
        sheet_usado = sheet_id or SHEET_ID
        cliente_usado = cliente_nombre or CLIENTE_NOMBRE

        # Override SHEET_ID temporalmente para este request
        os.environ['GOOGLE_SHEET_ID'] = sheet_usado

        resultado = procesar_factura(
            archivo_path=tmp_path,
            origen=origen,
            cliente_nombre=cliente_usado,
            drive_root_id=DRIVE_ROOT_ID,
        )
        return JSONResponse(content=resultado)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        Path(tmp_path).unlink(missing_ok=True)


# ── CORRECCIÓN MANUAL ─────────────────────────────────────────

@app.post("/corregir")
async def endpoint_corregir(req: CorreccionRequest):
    """
    Aplica una corrección manual a una factura y actualiza
    la memoria de proveedores para mejorar futuros aciertos.
    """
    if not SHEET_ID:
        raise HTTPException(status_code=500, detail="GOOGLE_SHEET_ID no configurado")

    nuevos_datos = {
        "proveedor_nombre": req.proveedor_nombre,
        "fecha_factura":    req.fecha_factura,
        "base_imponible":   req.base_imponible,
        "tipo_iva":         req.tipo_iva,
        "categoria":        req.categoria,
        "subcategoria":     req.subcategoria,
        "notas":            req.notas,
    }
    # Limpiar None
    nuevos_datos = {k: v for k, v in nuevos_datos.items() if v is not None}

    try:
        resultado = aplicar_correccion(SHEET_ID, req.fila, nuevos_datos)
        return JSONResponse(content=resultado)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── CHAT INTELIGENTE ──────────────────────────────────────────

@app.post("/chat")
async def endpoint_chat(req: ChatRequest):
    """
    Responde preguntas sobre gastos, IVA y facturas.
    Usa Claude API si está disponible, o respuestas directas de Sheets.
    """
    msg = req.mensaje.lower().strip()

    try:
        # Resumen de gastos
        if any(w in msg for w in ["resumen", "gasto", "cuánto", "cuanto", "total"]):
            r = obtener_resumen_mes()
            if not r["num_facturas"]:
                return {"respuesta": "No hay facturas registradas este mes todavía."}

            lineas = [f"📊 Resumen de {_nombre_mes()}:\n"]
            for cat, imp in r["por_categoria"].items():
                lineas.append(f"• {cat.replace('_',' ').title()}: {imp:.2f}€")
            lineas += [
                f"\nBase imponible: {r['total_base']:.2f}€",
                f"IVA soportado: {r['total_iva']:.2f}€",
                f"Total gastos: {r['total_gasto']:.2f}€",
            ]
            return {"respuesta": "\n".join(lineas)}

        # IVA
        if any(w in msg for w in ["iva", "trimestre", "303", "hacienda"]):
            r = obtener_iva_trimestre()
            meses = {1:"Ene–Mar", 2:"Abr–Jun", 3:"Jul–Sep", 4:"Oct–Dic"}
            respuesta = (
                f"📋 IVA Trimestre {r['trimestre']} ({meses[r['trimestre']]} {r['anio']}):\n\n"
                f"• IVA al 21%: {r['soportado_21']:.2f}€\n"
                f"• IVA al 10%: {r['soportado_10']:.2f}€\n"
                f"• IVA al 4%: {r['soportado_4']:.2f}€\n"
                f"\nTotal soportado: {r['total_soportado']:.2f}€\n\n"
                f"Este importe puedes deducirlo del IVA repercutido en tus ventas al presentar el modelo 303."
            )
            return {"respuesta": respuesta}

        # Pendientes
        if any(w in msg for w in ["pendiente", "revisar", "error", "mal", "corregir"]):
            pends = obtener_pendientes(limite=5)
            if not pends:
                return {"respuesta": "✅ No tienes facturas pendientes de revisión. Todo está verificado."}
            lineas = [f"⚠️ Tienes {len(pends)} facturas pendientes:\n"]
            for p in pends:
                conf = p.get("Confianza IA (%)", "?")
                lineas.append(
                    f"• {p.get('Proveedor','?')} — "
                    f"{float(p.get('Total (€)',0)):.2f}€ "
                    f"({p.get('Fecha','?')}) · {conf}% confianza"
                )
            lineas.append("\nHaz clic en ✏️ en la tabla para corregirlas.")
            return {"respuesta": "\n".join(lineas)}

        # Proveedor específico
        if any(w in msg for w in ["proveedor", "makro", "endesa", "factura de"]):
            return {
                "respuesta": (
                    "Puedo mostrarte facturas de un proveedor específico. "
                    "Usa el buscador de la tabla y escribe el nombre del proveedor. "
                    "También puedes filtrar por categoría y estado."
                )
            }

        # Ayuda general
        return {
            "respuesta": (
                "Puedo ayudarte con:\n\n"
                "📊 *Resumen* — gastos del mes por categoría\n"
                "📋 *IVA* — estado del trimestre y modelo 303\n"
                "⚠️ *Pendientes* — facturas que necesitan revisión\n"
                "📎 *Subir factura* — usa el botón de adjuntar\n\n"
                "También puedes corregir facturas mal leídas haciendo clic en ✏️ en la tabla."
            )
        }

    except Exception as e:
        return {"respuesta": f"Error al consultar los datos: {e}"}


# ── DATOS PARA EL DASHBOARD ───────────────────────────────────

@app.get("/resumen")
async def endpoint_resumen(mes: Optional[int] = None, anio: Optional[int] = None):
    try:
        return obtener_resumen_mes(anio=anio, mes=mes)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/iva")
async def endpoint_iva(trimestre: Optional[int] = None, anio: Optional[int] = None):
    try:
        return obtener_iva_trimestre(anio=anio, trimestre=trimestre)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/pendientes")
async def endpoint_pendientes(limite: int = 10):
    try:
        return {"pendientes": obtener_pendientes(limite=limite)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── HELPERS ───────────────────────────────────────────────────

def _nombre_mes():
    from datetime import date
    meses = ["enero","febrero","marzo","abril","mayo","junio",
             "julio","agosto","septiembre","octubre","noviembre","diciembre"]
    hoy = date.today()
    return f"{meses[hoy.month-1]} {hoy.year}"


# ── ARRANQUE ─────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    puerto = int(os.environ.get("PORT", 8000))
    print(f"🚀 Servidor Gestio arrancando en http://localhost:{puerto}")
    uvicorn.run("servidor:app", host="0.0.0.0", port=puerto, reload=False)
