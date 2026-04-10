"""
GESTIO · Drive Manager + Memoria de Proveedores
Compatible con Railway — lee credenciales desde variable de entorno
"""

import os, json, re, tempfile
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

HOJA_PROVEEDORES  = "Proveedores_Memoria"
CABECERAS_MEMORIA = [
    "Nombre","NIF","IVA_Habitual","Categoria","Subcategoria",
    "Total_Facturas","Aciertos_IA","Ultima_Correccion","Notas"
]
MESES_ES = {
    1:"01 - Enero",2:"02 - Febrero",3:"03 - Marzo",
    4:"04 - Abril",5:"05 - Mayo",6:"06 - Junio",
    7:"07 - Julio",8:"08 - Agosto",9:"09 - Septiembre",
    10:"10 - Octubre",11:"11 - Noviembre",12:"12 - Diciembre"
}


def _get_credentials():
    """Lee credenciales desde variable de entorno o archivo."""
    # En Railway: desde variable de entorno GOOGLE_CREDENTIALS_JSON
    creds_json = os.environ.get("GOOGLE_CREDENTIALS_JSON")
    if creds_json:
        info = json.loads(creds_json)
        return Credentials.from_service_account_info(info, scopes=SCOPES)
    # En local: desde archivo
    creds_file = os.environ.get("GOOGLE_CREDENTIALS_FILE", "credenciales_google.json")
    return Credentials.from_service_account_file(creds_file, scopes=SCOPES)


def _drive_service():
    return build("drive", "v3", credentials=_get_credentials())


def _sheets_service():
    return build("sheets", "v4", credentials=_get_credentials())


def _obtener_o_crear_carpeta(service, nombre: str, padre_id: str) -> str:
    nombre_safe = nombre.replace("'", "\\'")
    query = (
        f"name='{nombre_safe}' and "
        f"'{padre_id}' in parents and "
        f"mimeType='application/vnd.google-apps.folder' and "
        f"trashed=false"
    )
    res = service.files().list(
        q=query, fields="files(id,name)", pageSize=1,
        supportsAllDrives=True, includeItemsFromAllDrives=True,
    ).execute()
    archivos = res.get("files", [])
    if archivos:
        return archivos[0]["id"]
    metadata = {
        "name": nombre,
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [padre_id],
    }
    carpeta = service.files().create(
        body=metadata, fields="id", supportsAllDrives=True,
    ).execute()
    return carpeta["id"]


def _nombre_archivo(datos: dict, extension: str) -> str:
    fecha   = datos.get("fecha_factura", datetime.today().strftime("%Y-%m-%d"))
    prov    = datos.get("proveedor_nombre", "Desconocido")
    base    = float(datos.get("base_imponible", 0))
    iva_pct = float(datos.get("tipo_iva", 21))
    total   = round(base + base * iva_pct / 100, 2)
    prov_clean = re.sub(r'[\\/*?:"<>|]', "", prov).strip().replace(" ", "_")[:40]
    ext = extension.lstrip(".")
    return f"{fecha}_{prov_clean}_{total:.2f}EUR.{ext}"


def subir_factura_drive(
    archivo_path: str,
    datos: dict,
    carpeta_raiz_id: str,
    cliente_nombre: str = "Mi_Negocio"
) -> dict:
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

    id_cliente   = _obtener_o_crear_carpeta(service, cliente_nombre,  carpeta_raiz_id)
    id_año       = _obtener_o_crear_carpeta(service, año,              id_cliente)
    id_mes       = _obtener_o_crear_carpeta(service, mes,              id_año)
    id_proveedor = _obtener_o_crear_carpeta(service, proveedor,        id_mes)

    nombre_final = _nombre_archivo(datos, extension)
    mime_map = {
        ".pdf":  "application/pdf",
        ".jpg":  "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png":  "image/png",
    }
    mime_type = mime_map.get(extension, "application/octet-stream")

    metadata = {"name": nombre_final, "parents": [id_proveedor]}
    media    = MediaFileUpload(str(archivo), mimetype=mime_type, resumable=False)
    archivo_drive = service.files().create(
        body=metadata, media_body=media,
        fields="id,webViewLink", supportsAllDrives=True,
    ).execute()

    return {
        "file_id":   archivo_drive["id"],
        "file_name": nombre_final,
        "web_url":   archivo_drive.get("webViewLink", ""),
        "folder_id": id_proveedor,
        "ruta":      f"{cliente_nombre}/{año}/{mes}/{proveedor}/{nombre_final}",
    }


def _leer_memoria(sheet_id: str) -> list:
    service = _sheets_service()
    try:
        res   = service.spreadsheets().values().get(
            spreadsheetId=sheet_id, range=f"{HOJA_PROVEEDORES}!A:I").execute()
        filas = res.get("values", [])
        if len(filas) < 2:
            return []
        headers = filas[0]
        return [dict(zip(headers, fila)) for fila in filas[1:]]
    except Exception:
        return []


def _guardar_fila_memoria(sheet_id: str, datos_proveedor: dict):
    service = _sheets_service()
    memoria = _leer_memoria(sheet_id)
    nombre  = datos_proveedor.get("Nombre", "")
    nif     = datos_proveedor.get("NIF", "")
    idx     = None
    for i, p in enumerate(memoria):
        if (nif and p.get("NIF") == nif) or \
           (nombre and p.get("Nombre","").lower() == nombre.lower()):
            idx = i + 2
            break

    fila = [
        datos_proveedor.get("Nombre",""),
        datos_proveedor.get("NIF",""),
        datos_proveedor.get("IVA_Habitual","21"),
        datos_proveedor.get("Categoria","otros"),
        datos_proveedor.get("Subcategoria",""),
        datos_proveedor.get("Total_Facturas","1"),
        datos_proveedor.get("Aciertos_IA","1"),
        datetime.today().strftime("%Y-%m-%d"),
        datos_proveedor.get("Notas",""),
    ]

    if idx:
        service.spreadsheets().values().update(
            spreadsheetId=sheet_id,
            range=f"{HOJA_PROVEEDORES}!A{idx}:I{idx}",
            valueInputOption="USER_ENTERED",
            body={"values": [fila]}
        ).execute()
    else:
        service.spreadsheets().values().append(
            spreadsheetId=sheet_id,
            range=f"{HOJA_PROVEEDORES}!A:I",
            valueInputOption="USER_ENTERED",
            body={"values": [fila]}
        ).execute()


def buscar_proveedor_en_memoria(sheet_id: str, nombre: str = "", nif: str = ""):
    if not nombre and not nif:
        return None
    memoria = _leer_memoria(sheet_id)
    nombre_lower = nombre.lower()
    for p in memoria:
        if nif and p.get("NIF") == nif:
            return p
        if nombre and nombre_lower in p.get("Nombre","").lower():
            return p
        if nombre and p.get("Nombre","").lower() in nombre_lower:
            return p
    return None


def contexto_proveedor_para_ia(sheet_id: str, nombre: str = "", nif: str = "") -> str:
    prov = buscar_proveedor_en_memoria(sheet_id, nombre, nif)
    if not prov:
        return ""
    lineas = [
        "\nCONTEXTO DEL PROVEEDOR CONOCIDO:",
        f"- Nombre: {prov.get('Nombre')}",
        f"- IVA habitual: {prov.get('IVA_Habitual')}%",
        f"- Categoria: {prov.get('Categoria')}",
    ]
    if prov.get("Subcategoria"):
        lineas.append(f"- Subcategoria: {prov.get('Subcategoria')}")
    if prov.get("NIF"):
        lineas.append(f"- NIF: {prov.get('NIF')}")
    lineas.append("Usa estos datos como referencia prioritaria si la imagen es ambigua.")
    return "\n".join(lineas)


def registrar_factura_en_memoria(sheet_id: str, datos: dict, fue_corregida: bool = False):
    nombre = datos.get("proveedor_nombre", "")
    if not nombre or nombre == "Desconocido":
        return
    prov_actual = buscar_proveedor_en_memoria(
        sheet_id, nombre=nombre, nif=datos.get("proveedor_nif",""))
    total    = int(prov_actual.get("Total_Facturas",0))+1 if prov_actual else 1
    aciertos = int(prov_actual.get("Aciertos_IA",0)) if prov_actual else 0
    if not fue_corregida:
        aciertos += 1
    _guardar_fila_memoria(sheet_id, {
        "Nombre":         nombre,
        "NIF":            datos.get("proveedor_nif", prov_actual.get("NIF","") if prov_actual else ""),
        "IVA_Habitual":   str(datos.get("tipo_iva", 21)),
        "Categoria":      datos.get("categoria","otros"),
        "Subcategoria":   datos.get("subcategoria",""),
        "Total_Facturas": str(total),
        "Aciertos_IA":    str(aciertos),
        "Notas":          "corregida manualmente" if fue_corregida else "",
    })


def aplicar_correccion(sheet_id: str, fila_num: str, nuevos_datos: dict) -> dict:
    service = _sheets_service()
    col_map = {
        "fecha_factura":"A","proveedor_nombre":"B","descripcion":"E",
        "categoria":"F","subcategoria":"G","base_imponible":"H",
        "tipo_iva":"I","notas":"O",
    }
    fila = str(fila_num)
    for campo, col in col_map.items():
        if campo in nuevos_datos:
            service.spreadsheets().values().update(
                spreadsheetId=sheet_id,
                range=f"Facturas_Compras!{col}{fila}",
                valueInputOption="USER_ENTERED",
                body={"values": [[nuevos_datos[campo]]]}
            ).execute()
    service.spreadsheets().values().update(
        spreadsheetId=sheet_id,
        range=f"Facturas_Compras!L{fila}",
        valueInputOption="USER_ENTERED",
        body={"values": [["verificada"]]}
    ).execute()
    registrar_factura_en_memoria(sheet_id, nuevos_datos, fue_corregida=True)
    return {
        "exito": True,
        "fila_actualizada": fila,
        "mensaje": f"Corregida. La proxima de {nuevos_datos.get('proveedor_nombre','')} se procesara mejor."
    }
