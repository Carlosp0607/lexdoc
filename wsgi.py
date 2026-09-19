from app import app, init_db, resetear_demo, enviar_alertas, MODO_DEMO
from apscheduler.schedulers.background import BackgroundScheduler
import requests
import os
import logging

log = logging.getLogger('lexdoc')

init_db()

APP_URL = os.environ.get('APP_URL', 'https://lexdoc.onrender.com')

# Cada cuantas horas vuelve la demostracion a su estado inicial.
HORAS_RESET = int(os.environ.get('HORAS_RESET_DEMO', '12'))


def keep_alive():
    try:
        requests.get(APP_URL, timeout=10)
        log.info("Keep alive enviado")
    except Exception as e:
        log.warning("Keep alive error: %s", e)


scheduler = BackgroundScheduler()
scheduler.add_job(keep_alive, 'interval', minutes=14)
# El reinicio borra toda la base: solo existe en modo demo
if MODO_DEMO:
    scheduler.add_job(resetear_demo, 'interval', hours=HORAS_RESET)

# Alertas por correo: solo si hay clave de Resend configurada.
# El flag alerta_enviada evita repetir el correo de un mismo caso.
if os.environ.get('RESEND_API_KEY'):
    scheduler.add_job(enviar_alertas, 'interval', hours=1)
    log.info("Alertas por correo activas (cada hora)")

scheduler.start()
if MODO_DEMO:
    log.info("Reinicio de la demostracion cada %s horas", HORAS_RESET)

if __name__ == '__main__':
    app.run()
