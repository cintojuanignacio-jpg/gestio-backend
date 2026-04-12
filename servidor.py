import os
import shutil
import tempfile
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="Gestio Backend")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
SUPABASE_URL      = os.environ.get("SUPABASE_URL", "https://ulralnthmtrhlqgzlbaj.supabase.co")
SUPABASE_KEY      = os.environ.get("SUPABASE_SERVICE_KEY", "")
BACK_URL          = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "")

import anthropic
import base64
import json
import httpx
from datetime import datetime

# ── Supabase helper ────────────────────────────────────────────────────────────
def supabase_insert(table: str, data: dict) -> dict:
    """Insert a row into Supabase using the REST API."""
    url = f"{SUPABASE_URL}/rest/v1/{table}"
    headers = {
        "apikey":        SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type":  "application/json",
        "Prefer":        "return=representation"
    }
    r = httpx.post(url, json=data, headers=headers)
    r.raise_for_status()
    return r.json()

# ── Claude helper ──────────────────────────────────────────────────────────────
def analizar_factura_con_claude(archivo_path: str) -> dict:
    """Send invoice to Claude and get structured data back."""
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    ext = Path(archivo_path).suffix.lower()
    with open(archivo_path, "rb") as f:
        raw = f.read()
    b64 = base64.b64encode(raw).decode()

    if ext == ".pdf":
        media_type = "application/pdf"
        content_block = {
            "type": "document",
            "source": {"type": "base64", "media_type": media_type, "data": b64}
        }
    else:
        media_type = "image/jpeg" if ext in [".jpg", ".jpeg"] else "image/png"
        content_block = {
            "type": "image",
            "source": {"type": "base64", "media_type": media_type, "data": b64}
        }

    prompt = """Analiza esta factura y devuelve SOLO un JSON con estos campos exactos:
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
        messages=[{
            "role": "user",
            "content": [content_block, {"type": "text", "text": prompt}]
        }]
    )

    text = response.content[0].text.strip()
    # Clean markdown code blocks if present
    text = text.replace("```json", "").replace("```", "").strip()
    return json.loads(text)

# ── Endpoints ──────────────────────────────────────────────────────────────────
@app.get("/health")
def health():
    return {"estado": "ok", "version": "2.0"}

@app.post("/procesar-factura")
async def procesar_factura(
    archivo:        UploadFile = File(...),
    origen:         str        = Form(default="web"),
    user_id:        str        = Form(default=""),
    cliente_nombre: str        = Form(default="")
):
    tmp_path = None
    try:
        # Save file temporarily
        ext = Path(archivo.filename).suffix.lower()
        with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as tmp:
            shutil.copyfileobj(archivo.file, tmp)
            tmp_path = tmp.name

        # Analyze with Claude
        datos = analizar_factura_con_claude(tmp_path)

        # Build row for Supabase
        row = {
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
            "estado":         "pendiente",
            "confianza_ia":   float(datos.get("confianza_ia") or 0),
            "canal":          origen,
        }

        # Add user_id if provided
        if user_id:
            row["user_id"] = user_id

        # Insert into Supabase if we have service key and user_id
        guardado = False
        if SUPABASE_KEY and user_id:
            try:
                supabase_insert("facturas", row)
                guardado = True
            except Exception as e:
                print(f"Error guardando en Supabase: {e}")

        # Build response message
        msg = f"""✅ Factura procesada correctamente

📅 Fecha: {datos.get('fecha', 'No detectada')}
🏪 Proveedor: {datos.get('proveedor', 'No detectado')}
📋 Descripcion: {datos.get('descripcion', '-')}
🏷️ Categoria: {datos.get('categoria', 'otros')}

💰 Base imponible: {datos.get('base_imponible', 0)} EUR
📊 IVA ({datos.get('iva_pct', 21)}%): {datos.get('cuota_iva', 0)} EUR
💵 Total: {datos.get('total', 0)} EUR

🤖 Confianza IA: {datos.get('confianza_ia', 0)}%
{'✅ Guardada en base de datos' if guardado else '⚠️ No se pudo guardar (falta configuracion)'}"""

        return {
            "exito":   True,
            "mensaje": msg,
            "datos":   datos,
            "guardado": guardado
        }

    except json.JSONDecodeError as e:
        return {"exito": False, "mensaje": f"Error al interpretar la factura: {str(e)}"}
    except Exception as e:
        return {"exito": False, "mensaje": f"Error al procesar: {str(e)}"}
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.remove(tmp_path)

@app.post("/chat")
async def chat(data: dict):
    """Simple chat endpoint for dashboard questions."""
    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        mensaje = data.get("mensaje", "")
        response = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=500,
            system="Eres un asistente de facturacion para comercios espanoles. Responde de forma concisa y util sobre IVA, gastos, facturas y contabilidad.",
            messages=[{"role": "user", "content": mensaje}]
        )
        return {"respuesta": response.content[0].text}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/facturas/{user_id}")
async def get_facturas(user_id: str):
    """Get all invoices for a user."""
    try:
        url = f"{SUPABASE_URL}/rest/v1/facturas?user_id=eq.{user_id}&order=fecha.desc"
        headers = {
            "apikey":        SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
        }
        r = httpx.get(url, headers=headers)
        r.raise_for_status()
        return {"facturas": r.json()}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/debug-creds")
async def debug_creds():
    return {
        "supabase_url":    SUPABASE_URL,
        "supabase_key_ok": bool(SUPABASE_KEY),
        "anthropic_ok":    bool(ANTHROPIC_API_KEY),
        "version":         "2.0"
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
