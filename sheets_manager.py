"""
╔══════════════════════════════════════════════════════════════╗
║  CAFETERÍA MADRID · Gestor de Google Sheets                  ║
║  Módulo: sheets_manager.py                                   ║
║                                                              ║
║  Qué hace:                                                   ║
║  - Crea y formatea las hojas automáticamente                 ║
║  - Graba cada factura como una fila nueva                    ║
║  - Genera resúmenes mensuales e IVA trimestral               ║
╚══════════════════════════════════════════════════════════════╝
"""

import os
from datetime import date, datetime
from dotenv import load_dotenv
import gspread
from google.oauth2.service_account import Credentials

load_dotenv()

# ── Scopes necesarios para leer y escribir Sheets ───────────
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# ── Nombres de las hojas ─────────────────────────────────────
HOJA_FACTURAS  = "Facturas_Compras"
HOJA_RESUMEN   = "Resumen_Mensual"
HOJA_IVA       = "IVA_Trimestral"

# ── Cabeceras de la hoja principal ──────────────────────────
CABECERAS = [
    "Fecha", "Proveedor", "NIF Proveedor", "Nº Factura",
    "Descripción", "Categoría", "Subcategoría",
    "Base Imponible (€)", "IVA %", "Cuota IVA (€)", "Total (€)",
    "Estado", "Origen", "Confianza IA (%)", "Notas"
]


def _conectar() -> gspread.Spreadsheet:
    """Abre el Google Sheet usando las credenciales de la cuenta de servicio."""
    creds_file = os.environ["GOOGLE_CREDENTIALS_FILE"]
    sheet_id   = os.environ["GOOGLE_SHEET_ID"]

    creds  = Credentials.from_service_account_file(creds_file, scopes=SCOPES)
    client = gspread.authorize(creds)
    return client.open_by_key(sheet_id)


def inicializar_sheets():
    """
    Crea las hojas si no existen y pone las cabeceras y fórmulas.
    Ejecutar una sola vez al configurar.
    """
    libro = _conectar()
    hojas_existentes = [h.title for h in libro.worksheets()]

    # ── Hoja 1: Facturas_Compras ─────────────────────────────
    if HOJA_FACTURAS not in hojas_existentes:
        ws = libro.add_worksheet(title=HOJA_FACTURAS, rows=1000, cols=15)
    else:
        ws = libro.worksheet(HOJA_FACTURAS)

    # Poner cabeceras si la fila 1 está vacía
    if not ws.row_values(1):
        ws.update("A1:O1", [CABECERAS])
        # Formato cabecera: negrita y fondo verde oscuro
        ws.format("A1:O1", {
            "textFormat": {"bold": True, "foregroundColor": {"red": 1, "green": 1, "blue": 1}},
            "backgroundColor": {"red": 0.06, "green": 0.62, "blue": 0.35},
            "horizontalAlignment": "CENTER"
        })
        # Congelar fila 1 para que siempre sea visible al hacer scroll
        ws.freeze(rows=1)
        # Ancho de columnas clave
        _ajustar_columnas(ws)

    # ── Hoja 2: Resumen_Mensual ──────────────────────────────
    if HOJA_RESUMEN not in hojas_existentes:
        ws2 = libro.add_worksheet(title=HOJA_RESUMEN, rows=50, cols=8)
        _crear_hoja_resumen(ws2)

    # ── Hoja 3: IVA_Trimestral ───────────────────────────────
    if HOJA_IVA not in hojas_existentes:
        ws3 = libro.add_worksheet(title=HOJA_IVA, rows=30, cols=6)
        _crear_hoja_iva(ws3)

    # Eliminar la hoja "Sheet1" por defecto si existe
    for h in libro.worksheets():
        if h.title in ("Sheet1", "Hoja 1", "Hoja1"):
            try:
                libro.del_worksheet(h)
            except Exception:
                pass

    print("✅ Google Sheets inicializado correctamente.")
    return True


def grabar_factura(datos: dict, origen: str = "telegram") -> int:
    """
    Añade una fila nueva en Facturas_Compras con los datos de la factura.

    Parámetros:
        datos  : dict con los campos extraídos por la IA
        origen : 'telegram' | 'manual' | 'ia_pdf' | 'ia_email'

    Devuelve:
        número de fila donde se grabó
    """
    libro = _conectar()
    ws    = libro.worksheet(HOJA_FACTURAS)

    base    = float(datos.get("base_imponible", 0))
    iva_pct = float(datos.get("tipo_iva", 21))
    cuota   = round(base * iva_pct / 100, 2)
    total   = round(base + cuota, 2)
    conf    = int(float(datos.get("confianza", 0)) * 100)

    # Estado: pendiente si confianza < 75% o hay notas de revisión
    estado = "pendiente" if (conf < 75 or datos.get("notas_revision")) else "verificada"

    fila = [
        datos.get("fecha_factura", date.today().isoformat()),
        datos.get("proveedor_nombre", "Desconocido"),
        datos.get("proveedor_nif", ""),
        datos.get("numero_factura", ""),
        datos.get("descripcion", ""),
        datos.get("categoria", "otros"),
        datos.get("subcategoria", ""),
        base,
        iva_pct,
        cuota,
        total,
        estado,
        origen,
        conf,
        datos.get("notas_revision", ""),
    ]

    # Añadir al final de la hoja
    ws.append_row(fila, value_input_option="USER_ENTERED")

    # Número de fila recién añadida
    num_filas = len(ws.get_all_values())

    # Colorear la fila según estado
    color_fila = (
        {"red": 0.88, "green": 0.96, "blue": 0.88}  # verde claro = verificada
        if estado == "verificada"
        else {"red": 1.0, "green": 0.95, "blue": 0.8}  # amarillo = pendiente
    )
    ws.format(f"A{num_filas}:O{num_filas}", {
        "backgroundColor": color_fila
    })

    return num_filas


def obtener_resumen_mes(anio: int = None, mes: int = None) -> dict:
    """
    Calcula el resumen de gastos de un mes leyendo Facturas_Compras.
    Si no se pasan anio/mes, usa el mes actual.
    """
    hoy  = date.today()
    anio = anio or hoy.year
    mes  = mes  or hoy.month

    libro = _conectar()
    ws    = libro.worksheet(HOJA_FACTURAS)
    filas = ws.get_all_records()  # lista de dicts usando fila 1 como cabecera

    total_base = total_iva = total_gasto = 0
    por_categoria: dict[str, float] = {}
    num_facturas = 0

    for f in filas:
        fecha_str = str(f.get("Fecha", ""))
        if not fecha_str:
            continue
        try:
            fecha = datetime.strptime(fecha_str, "%Y-%m-%d").date()
        except ValueError:
            continue

        if fecha.year != anio or fecha.month != mes:
            continue
        if str(f.get("Estado", "")).lower() == "rechazada":
            continue

        base  = float(str(f.get("Base Imponible (€)", 0)).replace(",", ".") or 0)
        iva   = float(str(f.get("Cuota IVA (€)", 0)).replace(",", ".") or 0)
        tot   = float(str(f.get("Total (€)", 0)).replace(",", ".") or 0)
        cat   = str(f.get("Categoría", "otros"))

        total_base  += base
        total_iva   += iva
        total_gasto += tot
        por_categoria[cat] = por_categoria.get(cat, 0) + tot
        num_facturas += 1

    return {
        "anio":           anio,
        "mes":            mes,
        "num_facturas":   num_facturas,
        "total_base":     round(total_base, 2),
        "total_iva":      round(total_iva, 2),
        "total_gasto":    round(total_gasto, 2),
        "por_categoria":  {k: round(v, 2) for k, v in
                           sorted(por_categoria.items(), key=lambda x: -x[1])},
    }


def obtener_iva_trimestre(anio: int = None, trimestre: int = None) -> dict:
    """
    Calcula el IVA soportado del trimestre leyendo Facturas_Compras.
    """
    hoy       = date.today()
    anio      = anio      or hoy.year
    trimestre = trimestre or ((hoy.month - 1) // 3 + 1)

    mes_inicio = (trimestre - 1) * 3 + 1
    mes_fin    = trimestre * 3

    libro = _conectar()
    ws    = libro.worksheet(HOJA_FACTURAS)
    filas = ws.get_all_records()

    soportado_21 = soportado_10 = soportado_4 = 0

    for f in filas:
        fecha_str = str(f.get("Fecha", ""))
        if not fecha_str:
            continue
        try:
            fecha = datetime.strptime(fecha_str, "%Y-%m-%d").date()
        except ValueError:
            continue

        if fecha.year != anio or not (mes_inicio <= fecha.month <= mes_fin):
            continue
        if str(f.get("Estado", "")).lower() == "rechazada":
            continue

        iva_pct = float(str(f.get("IVA %", 21)).replace(",", ".") or 21)
        cuota   = float(str(f.get("Cuota IVA (€)", 0)).replace(",", ".") or 0)

        if iva_pct == 21:
            soportado_21 += cuota
        elif iva_pct == 10:
            soportado_10 += cuota
        elif iva_pct == 4:
            soportado_4  += cuota

    total = soportado_21 + soportado_10 + soportado_4

    return {
        "anio":          anio,
        "trimestre":     trimestre,
        "soportado_21":  round(soportado_21, 2),
        "soportado_10":  round(soportado_10, 2),
        "soportado_4":   round(soportado_4,  2),
        "total_soportado": round(total, 2),
    }


def obtener_pendientes(limite: int = 5) -> list[dict]:
    """Devuelve las facturas con estado 'pendiente', ordenadas por confianza ascendente."""
    libro = _conectar()
    ws    = libro.worksheet(HOJA_FACTURAS)
    filas = ws.get_all_records()

    pendientes = [
        f for f in filas
        if str(f.get("Estado", "")).lower() == "pendiente"
    ]
    # Ordenar por confianza ascendente (las menos seguras primero)
    pendientes.sort(key=lambda x: float(str(x.get("Confianza IA (%)", 100)).replace(",", ".") or 100))
    return pendientes[:limite]


# ── Helpers internos ─────────────────────────────────────────

def _ajustar_columnas(ws: gspread.Worksheet):
    """Ajusta el ancho de las columnas principales."""
    requests = [
        {"updateDimensionProperties": {
            "range": {"sheetId": ws.id, "dimension": "COLUMNS",
                      "startIndex": i, "endIndex": i + 1},
            "properties": {"pixelSize": w},
            "fields": "pixelSize"
        }}
        for i, w in enumerate([90, 160, 110, 100, 200, 110, 110, 110, 60, 100, 80, 85, 75, 90, 180])
    ]
    ws.spreadsheet.batch_update({"requests": requests})


def _crear_hoja_resumen(ws: gspread.Worksheet):
    """Inicializa la hoja Resumen_Mensual con cabeceras."""
    cabeceras = ["Mes", "Nº Facturas", "Base Total (€)",
                 "IVA Soportado (€)", "Total Gastos (€)"]
    ws.update("A1:E1", [cabeceras])
    ws.format("A1:E1", {
        "textFormat": {"bold": True, "foregroundColor": {"red": 1, "green": 1, "blue": 1}},
        "backgroundColor": {"red": 0.26, "green": 0.52, "blue": 0.96},
        "horizontalAlignment": "CENTER"
    })
    ws.freeze(rows=1)


def _crear_hoja_iva(ws: gspread.Worksheet):
    """Inicializa la hoja IVA_Trimestral con cabeceras."""
    cabeceras = ["Trimestre", "IVA Soportado 21%", "IVA Soportado 10%",
                 "IVA Soportado 4%", "Total Soportado (€)", "Estado"]
    ws.update("A1:F1", [cabeceras])
    ws.format("A1:F1", {
        "textFormat": {"bold": True, "foregroundColor": {"red": 1, "green": 1, "blue": 1}},
        "backgroundColor": {"red": 0.96, "green": 0.60, "blue": 0.0},
        "horizontalAlignment": "CENTER"
    })
    ws.freeze(rows=1)


# ── Ejecución directa: inicializa las hojas ──────────────────
if __name__ == "__main__":
    inicializar_sheets()
