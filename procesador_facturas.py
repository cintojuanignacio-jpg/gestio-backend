"""
╔══════════════════════════════════════════════════════════════╗
║  GESTIO · Procesador de Facturas con IA  v2                  ║
║  Con memoria de proveedores + Google Drive                   ║
╚══════════════════════════════════════════════════════════════╝
"""

import os, base64, json, re
from datetime import date, datetime
from pathlib import Path

import anthropic
from dotenv import load_dotenv
from sheets_manager import grabar_factura
from drive_manager import (
    subir_factura_drive,
    contexto_proveedor_para_ia,
    registrar_factura_en_memoria,
)

load_dotenv()
claude = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

SHEET_ID       = os.environ.get("GOOGLE_SHEET_ID", "")
DRIVE_ROOT_ID  = os.environ.get("GOOGLE_DRIVE_ROOT_ID", "")
CLIENTE_NOMBRE = os.environ.get("CLIENTE_NOMBRE", "Mi_Negocio")

CATEGORIAS_VALIDAS = {
    "materia_prima","suministros","servicios",
    "alquiler","personal","mantenimiento","otros"
}
TIPOS_IVA_VALIDOS = {0.0, 4.0, 10.0, 21.0}

PROMPT_BASE = """Eres un asistente contable especializado en fiscalidad española.
Analiza este documento (factura, ticket o albarán) de un comercio en España y extrae los datos fiscales.

REGLAS IVA ESPAÑA:
- 21% suministros (luz, gas, teléfono), servicios digitales, material oficina
- 10% alimentos preparados, bebidas con alcohol, hostelería
- 4%  pan, harinas, leche, huevos, frutas, verduras, café/té sin preparar, agua
- 0%  seguros, servicios financieros

CATEGORÍAS: materia_prima, suministros, servicios, alquiler, personal, mantenimiento, otros

{contexto_proveedor}

Devuelve ÚNICAMENTE un JSON válido (sin texto adicional, sin markdown):
{{
  "proveedor_nombre": "nombre comercial del emisor",
  "proveedor_nif": "CIF/NIF o null",
  "numero_factura": "número o null",
  "fecha_factura": "YYYY-MM-DD",
  "base_imponible": 0.00,
  "tipo_iva": 21.0,
  "descripcion": "descripción breve en 1 línea",
  "categoria": "una de las categorías",
  "subcategoria": "detalle específico",
  "confianza": 0.95,
  "notas_revision": "texto si algo no está claro, null si OK"
}}"""


def procesar_factura(
    archivo_path: str,
    origen: str = "telegram",
    cliente_nombre: str = None,
    drive_root_id: str = None,
) -> dict:
    archivo = Path(archivo_path)
    if not archivo.exists():
        return {"exito": False, "error": f"Archivo no encontrado: {archivo_path}"}

    cliente = cliente_nombre or CLIENTE_NOMBRE
    root_id = drive_root_id  or DRIVE_ROOT_ID

    try:
        img_b64, media_type = _preparar(archivo)
    except Exception as e:
        return {"exito": False, "error": f"Error al leer el archivo: {e}"}

    # Primera pasada sin contexto específico
    try:
        raw, tokens = _llamar_claude(img_b64, media_type, archivo.suffix.lower(), "")
        datos = _validar(_parsear(raw))
    except Exception as e:
        return {"exito": False, "error": f"Error en Claude API: {e}"}

    # Segunda pasada con contexto del proveedor si la confianza es baja
    if SHEET_ID and datos.get("proveedor_nombre") and datos["confianza"] < 0.80:
        ctx = contexto_proveedor_para_ia(
            SHEET_ID,
            nombre=datos.get("proveedor_nombre", ""),
            nif=datos.get("proveedor_nif", "")
        )
        if ctx:
            try:
                raw2, t2 = _llamar_claude(img_b64, media_type, archivo.suffix.lower(), ctx)
                datos2 = _validar(_parsear(raw2))
                if datos2["confianza"] > datos["confianza"]:
                    datos = datos2
                tokens += t2
            except Exception:
                pass

    # Subir a Google Drive
    drive_info = {}
    if root_id:
        try:
            drive_info = subir_factura_drive(
                archivo_path=str(archivo),
                datos=datos,
                carpeta_raiz_id=root_id,
                cliente_nombre=cliente,
            )
            datos["archivo_url"] = drive_info.get("web_url", "")
        except Exception as e:
            drive_info = {"error": str(e)}

    # Grabar en Google Sheets
    fila = None
    if SHEET_ID:
        try:
            fila = grabar_factura(datos, origen=origen)
        except Exception as e:
            return {"exito": False, "error": f"Error al grabar en Sheets: {e}", "datos": datos}

    # Actualizar memoria de proveedores
    if SHEET_ID:
        try:
            registrar_factura_en_memoria(SHEET_ID, datos, fue_corregida=False)
        except Exception:
            pass

    return {
        "exito":             True,
        "mensaje":           _mensaje(datos, fila, drive_info),
        "fila":              fila,
        "drive":             drive_info,
        "tokens":            tokens,
        "datos":             datos,
        "requiere_revision": datos["confianza"] < 0.75 or bool(datos.get("notas_revision")),
    }


def _preparar(archivo: Path) -> tuple:
    ext = archivo.suffix.lower()
    if ext == ".pdf":
        from pdf2image import convert_from_path
        from io import BytesIO
        buf = BytesIO()
        convert_from_path(str(archivo), dpi=200)[0].save(buf, format="JPEG", quality=85)
        return base64.standard_b64encode(buf.getvalue()).decode(), "image/jpeg"
    elif ext in (".jpg", ".jpeg"):
        return base64.standard_b64encode(archivo.read_bytes()).decode(), "image/jpeg"
    elif ext == ".png":
        return base64.standard_b64encode(archivo.read_bytes()).decode(), "image/png"
    raise ValueError(f"Formato no soportado: {ext}")


def _llamar_claude(img_b64: str, media_type: str, ext: str, contexto: str) -> tuple:
    prompt = PROMPT_BASE.format(contexto_proveedor=contexto)
    r = claude.messages.create(
        model="claude-sonnet-4-6", max_tokens=1000,
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": img_b64}},
            {"type": "text",  "text": prompt + f"\n\nTipo: {'PDF' if ext == '.pdf' else 'imagen'}."}
        ]}]
    )
    return r.content[0].text, r.usage.input_tokens + r.usage.output_tokens


def _parsear(texto: str) -> dict:
    try:
        return json.loads(texto.strip())
    except json.JSONDecodeError:
        pass
    m = re.search(r'\{[\s\S]*\}', texto)
    if m:
        return json.loads(m.group())
    raise ValueError("Sin JSON válido en la respuesta")


def _validar(d: dict) -> dict:
    iva = float(d.get("tipo_iva", 21.0))
    if iva not in TIPOS_IVA_VALIDOS:
        iva = min(TIPOS_IVA_VALIDOS, key=lambda x: abs(x - iva))
        d["notas_revision"] = (d.get("notas_revision") or "") + f" | IVA corregido a {iva}%"
    d["tipo_iva"] = iva
    if d.get("categoria") not in CATEGORIAS_VALIDAS:
        d["categoria"] = "otros"
    d["base_imponible"] = round(abs(float(d.get("base_imponible", 0))), 2)
    d["confianza"] = max(0.0, min(1.0, float(d.get("confianza", 0.7))))
    try:
        datetime.strptime(d.get("fecha_factura", ""), "%Y-%m-%d")
    except (ValueError, TypeError):
        d["fecha_factura"] = date.today().isoformat()
        d["notas_revision"] = (d.get("notas_revision") or "") + " | fecha no encontrada"
        d["confianza"] = min(d["confianza"], 0.5)
    return d


def _mensaje(d: dict, fila, drive: dict) -> str:
    base  = d["base_imponible"]
    pct   = d["tipo_iva"]
    cuota = round(base * pct / 100, 2)
    total = round(base + cuota, 2)
    conf  = int(d["confianza"] * 100)
    emoji = "✅" if conf >= 75 else "⚠️"

    lineas = [
        f"{emoji} Factura procesada",
        f"Proveedor: {d.get('proveedor_nombre','Desconocido')}",
        f"Fecha: {d['fecha_factura']}",
        f"Base: {base:.2f}€  |  IVA {pct}%: {cuota:.2f}€  |  Total: {total:.2f}€",
        f"Categoría: {d['categoria']}" + (f" › {d['subcategoria']}" if d.get("subcategoria") else ""),
        f"Confianza IA: {conf}%",
    ]
    if drive.get("ruta"):
        lineas.append(f"Drive: {drive['ruta']}")
    if fila:
        lineas.append(f"Sheets: fila {fila}")
    if d.get("notas_revision"):
        lineas.append(f"\n⚠️ {d['notas_revision']}")
        lineas.append("Usa /corregir para ajustar los datos.")
    return "\n".join(lineas)
