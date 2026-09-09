# Imagen base ligera con Python.
FROM python:3.12-slim

WORKDIR /app

# Evita archivos .pyc y fuerza salida sin buffer, para ver los logs en vivo.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Se instalan primero las dependencias para aprovechar la cache de capas:
# mientras requirements.txt no cambie, Docker reutiliza esta capa.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# El contenedor no corre como root
RUN useradd --create-home aplicacion && chown -R aplicacion:aplicacion /app
USER aplicacion

EXPOSE 5000

# wsgi:app carga la aplicacion y el planificador de alertas y de reinicio
# de la demostracion. Un solo worker: el planificador no debe duplicarse.
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "1", "wsgi:app"]