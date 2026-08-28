from app import app, init_db, resetear_demo
from apscheduler.schedulers.background import BackgroundScheduler
import requests
import os

init_db()

APP_URL = os.environ.get('APP_URL', 'https://lexdoc.onrender.com')

# Cada cuantas horas se limpian los datos de demostracion.
HORAS_RESET_DEMO = int(os.environ.get('HORAS_RESET_DEMO', '12'))


def keep_alive():
    try:
        requests.get(APP_URL, timeout=10)
        print("Keep alive enviado")
    except Exception as e:
        print(f"Keep alive error: {e}")


scheduler = BackgroundScheduler()
scheduler.add_job(keep_alive, 'interval', minutes=14)
scheduler.add_job(resetear_demo, 'interval', hours=HORAS_RESET_DEMO)
scheduler.start()
print(f"Limpieza de datos demo cada {HORAS_RESET_DEMO} horas")

if __name__ == '__main__':
    app.run()
