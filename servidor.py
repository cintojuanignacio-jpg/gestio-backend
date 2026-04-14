import os
import shutil
import tempfile
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="Gestio Backend v2")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
SUPABASE_URL      = os.environ.get("SUPABASE_URL", "https://ulralnthmtrhlqgzlbaj.supabase.co")
SUPABASE_KEY      = os.environ.get("SUPABASE_SERVICE_KEY", "")

import anthropic
import base64
import json
import httpx
from datetime import datetime

# ── Supabase helpers ───────────────────────────────────────────────────────────
def sb_headers():
    return {
        "apikey":        SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type":  "application/json",
        "Prefer":        "return=representation"
    }

def sb_insert(table: str, data: dict):
    r = httpx.post(f"{SUPABASE_URL}/rest/v1/{table}", json=data, headers=sb_headers())
    r.raise_for_status()
    return r.json()

def sb_get(table: str, params: str):
    r = httpx.get(f"{SUPABASE_URL}/rest/v1/{table}?{params}", headers=sb_headers())
    r.raise_for_status()
    return r.json()

def log_auditoria(user_id: str, nombre_archivo: str, canal: str, accion: str,
                  datos: dict = None, motivo: str = None):
    try:
        row = {
            "user_id":       user_id or None,
            "nombre_archivo": nombre_archivo,
            "canal":         canal,
            "accion":        accion,
            "proveedor":     datos.get("proveedor") if datos else None,
            "fecha_factura": datos.get("fecha") if datos else None,
            "total":         float(datos.get("total", 0)) if datos else None,
            "confianza_ia":  float(datos.get("confianza_ia", 0)) if datos else None,
            "resultado_ia":  json.dumps(datos) if datos else None,
            "motivo":        motivo
        }
        sb_insert("logs_ia", row)
    except Exception as e:
        print(f"Error guardando log: {e}")

# ── Claude ─────────────────────────────────────────────────────────────────────
def analizar_factura_con_claude(archivo_path: str) -> dict:
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    ext = Path(archivo_path).suffix.lower()
    with open(archivo_path, "rb") as f:
        raw = f.read()
    b64 = base64.b64encode(raw).decode()

    if ext == ".pdf":
        content_block = {"type":"document","source":{"type":"base64","media_type":"application/pdf","data":b64}}
    else:
        mt = "image/jpeg" if ext in [".jpg",".jpeg"] else "image/png"
        content_block = {"type":"image","source":{"type":"base64","media_type":mt,"data":b64}}

    prompt = """Analiza esta factura y devuelve SOLO un JSON con estos campos:
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

    response = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=1000,
        messages=[{"role":"user","content":[content_block,{"type":"text","text":prompt}]}]
    )
    text = response.content[0].text.strip().replace("```json","").replace("```","").strip()
    return json.loads(text)

# ── Endpoints ──────────────────────────────────────────────────────────────────
@app.get("/health")
def health():
    return {"estado":"ok","version":"2.1"}

@app.post("/procesar-factura")
async def procesar_factura(
    archivo:        UploadFile = File(...),
    origen:         str        = Form(default="web"),
    user_id:        str        = Form(default=""),
    cliente_nombre: str        = Form(default="")
):
    tmp_path = None
    try:
        ext = Path(archivo.filename).suffix.lower()
        with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as tmp:
            shutil.copyfileobj(archivo.file, tmp)
            tmp_path = tmp.name

        # Analyze with Claude
        datos = analizar_factura_con_claude(tmp_path)

        row = {
            "fecha":          datos.get("fecha"),
            "proveedor":      datos.get("proveedor"),
            "nif_proveedor":  datos.get("nif_proveedor"),
            "numero_factura": datos.get("numero_factura"),
            "descripcion":    datos.get("descripcion"),
            "categoria":      datos.get("categoria","otros"),
            "base_imponible": float(datos.get("base_imponible") or 0),
            "iva_pct":        float(datos.get("iva_pct") or 21),
            "cuota_iva":      float(datos.get("cuota_iva") or 0),
            "total":          float(datos.get("total") or 0),
            "confianza_ia":   float(datos.get("confianza_ia") or 0),
            "canal":          origen,
            "eliminada":      False,
            "estado":         "verificada" if float(datos.get("confianza_ia",0)) >= 85 else "pendiente",
        }
        if user_id:
            row["user_id"] = user_id

        guardado   = False
        duplicado  = False

        if SUPABASE_KEY and user_id:
            try:
                # Check duplicate
                existing = sb_get("facturas",
                    f"user_id=eq.{user_id}&proveedor=eq.{row.get('proveedor','')}&fecha=eq.{row.get('fecha','')}&total=eq.{row.get('total',0)}&eliminada=eq.false")
                if existing:
                    duplicado = True
                    log_auditoria(user_id, archivo.filename, origen, "duplicada", datos,
                                  motivo="Misma fecha, proveedor y total ya existen")
                else:
                    sb_insert("facturas", row)
                    guardado = True
                    log_auditoria(user_id, archivo.filename, origen, "procesada", datos)
            except Exception as e:
                print(f"Error Supabase: {e}")
                log_auditoria(user_id, archivo.filename, origen, "error", datos, motivo=str(e))
        else:
            # No user_id — log anyway
            log_auditoria(user_id or None, archivo.filename, origen, "procesada_sin_usuario", datos)

        # Response message
        confianza = float(datos.get("confianza_ia",0))
        if duplicado:
            msg = f"Ya existe en el sistema."
        elif guardado:
            estado_txt = "Verificada" if confianza >= 85 else "Pendiente"
            msg = f"OK: {datos.get('proveedor','?')} | {datos.get('fecha','')} | {datos.get('total',0)} EUR | IVA {datos.get('iva_pct',21)}% | Confianza {int(confianza)}% | {estado_txt}"
        else:
            msg = "Error al guardar."

        return {
            "exito":     guardado,
            "duplicado": duplicado,
            "mensaje":   msg,
            "datos":     datos,
            "guardado":  guardado
        }

    except json.JSONDecodeError as e:
        log_auditoria(user_id or None, archivo.filename, origen, "error",
                      motivo=f"Claude no devolvio JSON valido: {str(e)}")
        return {"exito":False,"duplicado":False,"mensaje":f"No se pudo leer la factura. Intentalo de nuevo.","guardado":False}
    except Exception as e:
        log_auditoria(user_id or None, archivo.filename, origen, "error", motivo=str(e))
        return {"exito":False,"duplicado":False,"mensaje":f"Error: {str(e)}","guardado":False}
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.remove(tmp_path)

@app.post("/chat")
async def chat(data: dict):
    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        response = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=500,
            system="Eres un asistente de facturacion para comercios espanoles. Responde de forma concisa sobre IVA, gastos, facturas y contabilidad.",
            messages=[{"role":"user","content":data.get("mensaje","")}]
        )
        return {"respuesta": response.content[0].text}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.delete("/facturas/{factura_id}")
async def eliminar_factura(factura_id: str, user_id: str = ""):
    """Soft delete — marca como eliminada y registra en logs."""
    try:
        # Get factura data for log
        facturas = sb_get("facturas", f"id=eq.{factura_id}")
        factura = facturas[0] if facturas else {}

        # Soft delete
        r = httpx.patch(
            f"{SUPABASE_URL}/rest/v1/facturas?id=eq.{factura_id}",
            json={"eliminada": True, "eliminada_en": datetime.utcnow().isoformat()},
            headers=sb_headers()
        )
        r.raise_for_status()

        # Log
        log_auditoria(
            user_id or factura.get("user_id"),
            factura.get("numero_factura") or factura.get("descripcion",""),
            "web",
            "eliminada",
            {
                "proveedor":    factura.get("proveedor"),
                "fecha":        str(factura.get("fecha","")),
                "total":        factura.get("total"),
                "confianza_ia": factura.get("confianza_ia")
            },
            motivo="Eliminada por el usuario"
        )
        return {"exito": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/facturas/{user_id}")
async def get_facturas(user_id: str):
    try:
        data = sb_get("facturas",
            f"user_id=eq.{user_id}&eliminada=eq.false&order=fecha.desc")
        return {"facturas": data}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/logs/{user_id}")
async def get_logs(user_id: str):
    try:
        data = sb_get("logs_ia",
            f"user_id=eq.{user_id}&order=creado_en.desc&limit=50")
        return {"logs": data}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/admin/logs")
async def get_all_logs():
    try:
        data = sb_get("logs_ia", "order=creado_en.desc&limit=200")
        return {"logs": data}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/admin/tickets")
async def get_all_tickets():
    try:
        data = sb_get("tickets", "order=creado_en.desc")
        return {"tickets": data}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.patch("/admin/tickets/{ticket_id}")
async def actualizar_ticket(ticket_id: str, data: dict):
    try:
        r = httpx.patch(
            f"{SUPABASE_URL}/rest/v1/tickets?id=eq.{ticket_id}",
            json=data,
            headers=sb_headers()
        )
        r.raise_for_status()
        return {"exito": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/debug-creds")
async def debug_creds():
    return {
        "supabase_url":    SUPABASE_URL,
        "supabase_key_ok": bool(SUPABASE_KEY),
        "anthropic_ok":    bool(ANTHROPIC_API_KEY),
        "version":         "2.1"
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
