from app import app, init_db, resetear_demo, MODO_DEMO
from apscheduler.schedulers.background import BackgroundScheduler
import requests
import os

init_db()

# URL propia del servicio. Cada deploy define la suya en Render.
APP_URL = os.environ.get('APP_URL', 'https://lexdoc.onrender.com')

# Cada cuantas horas se limpia la demo. Solo aplica si MODO_DEMO esta activo.
HORAS_RESET_DEMO = int(os.environ.get('HORAS_RESET_DEMO', '12'))


def keep_alive():
    try:
        requests.get(APP_URL, timeout=10)
        print("Keep alive enviado")
    except Exception as e:
        print(f"Keep alive error: {e}")


scheduler = BackgroundScheduler()
scheduler.add_job(keep_alive, 'interval', minutes=14)

if MODO_DEMO:
    scheduler.add_job(resetear_demo, 'interval', hours=HORAS_RESET_DEMO)
    print(f"Modo demo activo — reset cada {HORAS_RESET_DEMO} horas")

scheduler.start()

if __name__ == '__main__':
    app.run()
