from app import app, init_db
from apscheduler.schedulers.background import BackgroundScheduler
import requests

init_db()

def keep_alive():
    try:
        requests.get('https://lexdoc.onrender.com')
        print("✅ Keep alive ping enviado")
    except Exception as e:
        print(f"❌ Keep alive error: {e}")

scheduler = BackgroundScheduler()
scheduler.add_job(keep_alive, 'interval', minutes=14)
scheduler.start()

if __name__ == '__main__':
    app.run()
