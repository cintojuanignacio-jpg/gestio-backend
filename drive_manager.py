"""
╔══════════════════════════════════════════════════════════════╗
║  GESTIO · Drive Manager + Memoria de Proveedores             ║
║  Módulo: drive_manager.py                                    ║
║                                                              ║
║  Qué hace:                                                   ║
║  1. Organiza facturas en Drive por año/mes/proveedor         ║
║  2. Renombra archivos con fecha, proveedor e importe         ║
║  3. Mantiene una memoria de proveedores conocidos            ║
║  4. Aprende de correcciones manuales                         ║
╚══════════════════════════════════════════════════════════════╝
"""

import os
import json
import re
from datetime import datetime
from pathlib import Path

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from dotenv import load_dotenv

load_dotenv()

SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets",
]

# Hoja de proveedores conocidos (pestaña en tu Google Sheet)
HOJA_PROVEEDORES = "Proveedores_Memoria"

# Cabeceras de la tabla de memoria
CABECERAS_MEMORIA = [
    "Nombre", "NIF", "IVA_Habitual", "Categoria", "Subcategoria",
    "Total_Facturas", "Aciertos_IA", "Ultima_Correccion", "Notas"
]

MESES_ES = {
    1:"01 - Enero", 2:"02 - Febrero", 3:"03 - Marzo",
    4:"04 - Abril", 5:"05 - Mayo", 6:"06 - Junio",
    7:"07 - Julio", 8:"08 - Agosto", 9:"09 - Septiembre",
    10:"10 - Octubre", 11:"11 - Noviembre", 12:"12 - Diciembre"
}


def _drive_service():
    creds = Credentials.from_service_account_file(
        os.environ["GOOGLE_CREDENTIALS_FILE"], scopes=SCOPES
    )
    return build("drive", "v3", credentials=creds)


def _sheets_service():
    creds = Credentials.from_service_account_file(
        os.environ["GOOGLE_CREDENTIALS_FILE"], scopes=SCOPES
    )
    return build("sheets", "v4", credentials=creds)


# ============================================================
# DRIVE — GESTIÓN DE CARPETAS Y ARCHIVOS
# ============================================================

def _obtener_o_crear_carpeta(service, nombre: str, padre_id: str) -> str:
    """Busca una carpeta por nombre dentro de un padre. Si no existe, la crea."""
    nombre_safe = nombre.replace("'", "\\'")
    query = (
        f"name='{nombre_safe}' and "
        f"'{padre_id}' in parents and "
        f"mimeType='application/vnd.google-apps.folder' and "
        f"trashed=false"
    )
    resultados = service.files().list(
        q=query, fields="files(id, name)", pageSize=1
    ).execute()

    archivos = resultados.get("files", [])
    if archivos:
        return archivos[0]["id"]

    # Crear carpeta nueva
    metadata = {
        "name": nombre,
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [padre_id],
    }
    carpeta = service.files().create(body=metadata, fields="id").execute()
    return carpeta["id"]


def _nombre_archivo(datos: dict, extension: str) -> str:
    """
    Genera el nombre del archivo con formato:
    YYYY-MM-DD_Proveedor_Total€.ext
    Ej: 2026-04-03_Makro_España_783.64€.pdf
    """
    fecha   = datos.get("fecha_factura", datetime.today().strftime("%Y-%m-%d"))
    prov    = datos.get("proveedor_nombre", "Desconocido")
    base    = float(datos.get("base_imponible", 0))
    iva_pct = float(datos.get("tipo_iva", 21))
    total   = round(base + base * iva_pct / 100, 2)

    # Limpiar caracteres problemáticos del nombre del proveedor
    prov_clean = re.sub(r'[\\/*?:"<>|]', "", prov).strip()
    prov_clean = prov_clean.replace(" ", "_")[:40]

    ext = extension.lstrip(".")
    return f"{fecha}_{prov_clean}_{total:.2f}€.{ext}"


def subir_factura_drive(
    archivo_path: str,
    datos: dict,
    carpeta_raiz_id: str,
    cliente_nombre: str = "Mi_Negocio"
) -> dict:
    """
    Sube una factura a Drive organizándola en carpetas:
    {cliente_nombre}/{año}/{mes}/{proveedor}/archivo.pdf

    Devuelve dict con folder_id, file_id, file_name, web_url
    """
    service   = _drive_service()
    archivo   = Path(archivo_path)
    extension = archivo.suffix.lower()

    fecha_str = datos.get("fecha_factura", datetime.today().strftime("%Y-%m-%d"))
    try:
        fecha = datetime.strptime(fecha_str, "%Y-%m-%d")
    except ValueError:
        fecha = datetime.today()

    proveedor = datos.get("proveedor_nombre", "Sin_Proveedor")
    año       = str(fecha.year)
    mes       = MESES_ES[fecha.month]

    # Crear jerarquía de carpetas
    id_cliente   = _obtener_o_crear_carpeta(service, cliente_nombre,  carpeta_raiz_id)
    id_año       = _obtener_o_crear_carpeta(service, año,              id_cliente)
    id_mes       = _obtener_o_crear_carpeta(service, mes,              id_año)
    id_proveedor = _obtener_o_crear_carpeta(service, proveedor,        id_mes)

    # Nombre del archivo
    nombre_final = _nombre_archivo(datos, extension)

    # Determinar MIME type
    mime_map = {
        ".pdf":  "application/pdf",
        ".jpg":  "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png":  "image/png",
    }
    mime_type = mime_map.get(extension, "application/octet-stream")

    # Subir archivo
    metadata = {"name": nombre_final, "parents": [id_proveedor]}
    media    = MediaFileUpload(str(archivo), mimetype=mime_type, resumable=False)
    archivo_drive = service.files().create(
        body=metadata, media_body=media, fields="id, webViewLink"
    ).execute()

    return {
        "file_id":    archivo_drive["id"],
        "file_name":  nombre_final,
        "web_url":    archivo_drive.get("webViewLink", ""),
        "folder_id":  id_proveedor,
        "ruta":       f"{cliente_nombre}/{año}/{mes}/{proveedor}/{nombre_final}",
    }


# ============================================================
# MEMORIA DE PROVEEDORES
# ============================================================

def _leer_memoria(sheet_id: str) -> list[dict]:
    """Lee la hoja Proveedores_Memoria y devuelve lista de dicts."""
    service = _sheets_service()
    try:
        resultado = service.spreadsheets().values().get(
            spreadsheetId=sheet_id,
            range=f"{HOJA_PROVEEDORES}!A:I"
        ).execute()
        filas = resultado.get("values", [])
        if len(filas) < 2:
            return []
        headers = filas[0]
        return [dict(zip(headers, fila)) for fila in filas[1:]]
    except Exception:
        return []


def _guardar_fila_memoria(sheet_id: str, datos_proveedor: dict):
    """Añade o actualiza una fila en Proveedores_Memoria."""
    service = _sheets_service()
    memoria = _leer_memoria(sheet_id)

    nombre = datos_proveedor.get("Nombre", "")
    nif    = datos_proveedor.get("NIF", "")

    # Buscar si ya existe (por NIF o por nombre)
    fila_existente = None
    idx_existente  = None
    for i, prov in enumerate(memoria):
        if (nif and prov.get("NIF") == nif) or \
           (nombre and prov.get("Nombre", "").lower() == nombre.lower()):
            fila_existente = prov
            idx_existente  = i + 2  # +2 por cabecera y base 1
            break

    fila = [
        datos_proveedor.get("Nombre", ""),
        datos_proveedor.get("NIF", ""),
        datos_proveedor.get("IVA_Habitual", "21"),
        datos_proveedor.get("Categoria", "otros"),
        datos_proveedor.get("Subcategoria", ""),
        datos_proveedor.get("Total_Facturas", "1"),
        datos_proveedor.get("Aciertos_IA", "1"),
        datetime.today().strftime("%Y-%m-%d"),
        datos_proveedor.get("Notas", ""),
    ]

    if idx_existente:
        # Actualizar fila existente
        service.spreadsheets().values().update(
            spreadsheetId=sheet_id,
            range=f"{HOJA_PROVEEDORES}!A{idx_existente}:I{idx_existente}",
            valueInputOption="USER_ENTERED",
            body={"values": [fila]}
        ).execute()
    else:
        # Añadir fila nueva
        service.spreadsheets().values().append(
            spreadsheetId=sheet_id,
            range=f"{HOJA_PROVEEDORES}!A:I",
            valueInputOption="USER_ENTERED",
            body={"values": [fila]}
        ).execute()


def buscar_proveedor_en_memoria(sheet_id: str, nombre: str = "", nif: str = "") -> dict | None:
    """
    Busca un proveedor en la memoria por nombre o NIF.
    Devuelve sus datos si existe, None si no.
    """
    if not nombre and not nif:
        return None

    memoria = _leer_memoria(sheet_id)
    nombre_lower = nombre.lower()

    for prov in memoria:
        if nif and prov.get("NIF") == nif:
            return prov
        if nombre and nombre_lower in prov.get("Nombre", "").lower():
            return prov
        if nombre and prov.get("Nombre", "").lower() in nombre_lower:
            return prov
    return None


def contexto_proveedor_para_ia(sheet_id: str, nombre: str = "", nif: str = "") -> str:
    """
    Genera el texto de contexto que se añade al prompt de Claude
    cuando se conoce al proveedor.
    """
    prov = buscar_proveedor_en_memoria(sheet_id, nombre, nif)
    if not prov:
        return ""

    lineas = [
        f"\nCONTEXTO DEL PROVEEDOR CONOCIDO:",
        f"- Nombre: {prov.get('Nombre')}",
        f"- IVA habitual: {prov.get('IVA_Habitual')}%",
        f"- Categoría: {prov.get('Categoria')}",
    ]
    if prov.get("Subcategoria"):
        lineas.append(f"- Subcategoría: {prov.get('Subcategoria')}")
    if prov.get("NIF"):
        lineas.append(f"- NIF: {prov.get('NIF')}")
    lineas.append("Usa estos datos como referencia prioritaria si la imagen es ambigua.")

    return "\n".join(lineas)


def registrar_factura_en_memoria(sheet_id: str, datos: dict, fue_corregida: bool = False):
    """
    Después de procesar una factura, actualiza la memoria del proveedor.
    Si fue_corregida=True, no cuenta como acierto de la IA.
    """
    nombre = datos.get("proveedor_nombre", "")
    if not nombre or nombre == "Desconocido":
        return

    # Leer estado actual del proveedor
    prov_actual = buscar_proveedor_en_memoria(
        sheet_id,
        nombre=nombre,
        nif=datos.get("proveedor_nif", "")
    )

    total   = int(prov_actual.get("Total_Facturas", 0)) + 1 if prov_actual else 1
    aciertos = int(prov_actual.get("Aciertos_IA", 0)) if prov_actual else 0
    if not fue_corregida:
        aciertos += 1

    _guardar_fila_memoria(sheet_id, {
        "Nombre":         nombre,
        "NIF":            datos.get("proveedor_nif", prov_actual.get("NIF", "") if prov_actual else ""),
        "IVA_Habitual":   str(datos.get("tipo_iva", 21)),
        "Categoria":      datos.get("categoria", "otros"),
        "Subcategoria":   datos.get("subcategoria", ""),
        "Total_Facturas": str(total),
        "Aciertos_IA":    str(aciertos),
        "Notas":          "corregida manualmente" if fue_corregida else "",
    })


def aplicar_correccion(sheet_id: str, factura_id_o_fila: str, nuevos_datos: dict) -> dict:
    """
    Aplica una corrección manual a una factura existente en Sheets
    y actualiza la memoria del proveedor.

    nuevos_datos puede incluir: proveedor_nombre, tipo_iva, categoria,
    subcategoria, base_imponible, fecha_factura, notas
    """
    service = _sheets_service()

    # 1. Actualizar la fila de la factura en Facturas_Compras
    # Leer todas las filas para encontrar la correcta
    resultado = service.spreadsheets().values().get(
        spreadsheetId=sheet_id,
        range="Facturas_Compras!A:O"
    ).execute()
    filas = resultado.get("values", [])

    fila_idx = None
    for i, fila in enumerate(filas[1:], start=2):
        # Identificar por número de fila o por proveedor+fecha
        if str(i) == str(factura_id_o_fila):
            fila_idx = i
            break

    if fila_idx:
        # Actualizar campos corregidos
        campos_actualizar = {}
        col_map = {
            "fecha_factura":   "A",
            "proveedor_nombre":"B",
            "descripcion":     "E",
            "categoria":       "F",
            "subcategoria":    "G",
            "base_imponible":  "H",
            "tipo_iva":        "I",
            "notas":           "O",
        }
        for campo, col in col_map.items():
            if campo in nuevos_datos:
                service.spreadsheets().values().update(
                    spreadsheetId=sheet_id,
                    range=f"Facturas_Compras!{col}{fila_idx}",
                    valueInputOption="USER_ENTERED",
                    body={"values": [[nuevos_datos[campo]]]}
                ).execute()

        # Marcar como verificada
        service.spreadsheets().values().update(
            spreadsheetId=sheet_id,
            range=f"Facturas_Compras!L{fila_idx}",
            valueInputOption="USER_ENTERED",
            body={"values": [["verificada"]]}
        ).execute()

    # 2. Actualizar memoria del proveedor (marca como corregida)
    registrar_factura_en_memoria(sheet_id, nuevos_datos, fue_corregida=True)

    return {
        "exito": True,
        "fila_actualizada": fila_idx,
        "mensaje": f"Factura corregida. La próxima factura de {nuevos_datos.get('proveedor_nombre','este proveedor')} se procesará mejor."
    }


def inicializar_hoja_memoria(sheet_id: str):
    """Crea la pestaña Proveedores_Memoria si no existe."""
    service = _sheets_service()

    # Obtener hojas existentes
    meta = service.spreadsheets().get(spreadsheetId=sheet_id).execute()
    hojas = [h["properties"]["title"] for h in meta.get("sheets", [])]

    if HOJA_PROVEEDORES not in hojas:
        # Crear hoja nueva
        service.spreadsheets().batchUpdate(
            spreadsheetId=sheet_id,
            body={"requests": [{"addSheet": {"properties": {"title": HOJA_PROVEEDORES}}}]}
        ).execute()

        # Añadir cabeceras
        service.spreadsheets().values().update(
            spreadsheetId=sheet_id,
            range=f"{HOJA_PROVEEDORES}!A1:I1",
            valueInputOption="USER_ENTERED",
            body={"values": [CABECERAS_MEMORIA]}
        ).execute()

        # Formato cabecera
        service.spreadsheets().batchUpdate(
            spreadsheetId=sheet_id,
            body={"requests": [{
                "repeatCell": {
                    "range": {"sheetId": _obtener_sheet_id(service, sheet_id, HOJA_PROVEEDORES),
                              "startRowIndex": 0, "endRowIndex": 1},
                    "cell": {"userEnteredFormat": {
                        "textFormat": {"bold": True, "foregroundColor": {"red":1,"green":1,"blue":1}},
                        "backgroundColor": {"red":0.06,"green":0.62,"blue":0.35}
                    }},
                    "fields": "userEnteredFormat(textFormat,backgroundColor)"
                }
            }]}
        ).execute()

        print(f"✅ Hoja '{HOJA_PROVEEDORES}' creada correctamente.")
    else:
        print(f"ℹ️  Hoja '{HOJA_PROVEEDORES}' ya existe.")


def _obtener_sheet_id(service, spreadsheet_id: str, titulo: str) -> int:
    meta = service.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
    for h in meta.get("sheets", []):
        if h["properties"]["title"] == titulo:
            return h["properties"]["sheetId"]
    return 0


if __name__ == "__main__":
    sheet_id = os.environ.get("GOOGLE_SHEET_ID", "")
    if sheet_id:
        inicializar_hoja_memoria(sheet_id)
    else:
        print("ERROR: GOOGLE_SHEET_ID no configurado en .env")
