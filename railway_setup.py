"""
railway_setup.py — Ejecutar ANTES de arrancar el servidor en Railway
Crea el archivo credenciales_google.json desde la variable de entorno
"""
import os
import json

creds_json = os.environ.get('GOOGLE_CREDENTIALS_JSON')
if creds_json:
    with open('credenciales_google.json', 'w') as f:
        f.write(creds_json)
    print("credenciales_google.json creado OK")
else:
    print("ADVERTENCIA: GOOGLE_CREDENTIALS_JSON no encontrada")
