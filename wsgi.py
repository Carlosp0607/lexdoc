from app import app, init_db, enviar_alertas
from apscheduler.schedulers.background import BackgroundScheduler

init_db()

scheduler = BackgroundScheduler()
scheduler.add_job(enviar_alertas, 'interval', minutes=1)
scheduler.start()

if __name__ == '__main__':
    app.run()