from flask import (Flask, render_template, request, redirect, url_for, session,
                   flash, Response, g, abort, has_app_context)
from flask_wtf.csrf import CSRFProtect, CSRFError
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
import psycopg2
import psycopg2.extras
import os
import uuid
import hashlib
import logging
import secrets
import resend
from datetime import datetime, timedelta

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s %(levelname)s %(name)s: %(message)s')
log = logging.getLogger('lexdoc')

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY')
if not app.secret_key:
    raise RuntimeError('Falta la variable de entorno SECRET_KEY')

# Cookie de sesion: invisible para JavaScript, no viaja en POST desde otros
# sitios y, en Render (HTTPS), solo viaja cifrada.
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_SECURE'] = os.environ.get('RENDER') == 'true'
# La sesion vence tras 30 minutos sin actividad; cada peticion la renueva.
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(minutes=30)
app.config['SESSION_REFRESH_EACH_REQUEST'] = True

# URL publica para los enlaces de los correos (no se toma del encabezado Host,
# que el cliente puede falsificar)
APP_URL = os.environ.get('APP_URL', 'https://lexdoc.onrender.com').rstrip('/')

# Archivos: maximo 10 MB por peticion. Se guardan en PostgreSQL, no en disco,
# porque el disco de Render gratis se borra en cada reinicio.
app.config['MAX_CONTENT_LENGTH'] = 10 * 1024 * 1024

# Modo demostracion: activo salvo que MODO_DEMO=0. En demo hay acceso de
# invitado, datos ficticios, reinicio periodico y las descargas son PDF de prueba.
MODO_DEMO = os.environ.get('MODO_DEMO', '1') != '0'

# Token CSRF en todos los formularios POST
csrf = CSRFProtect(app)

# Valores que el servidor acepta (el HTML se puede alterar, el servidor no)
ROLES_ASIGNABLES = {'abogado', 'jefe'}
ESTADOS_CASO = {'pendiente', 'en_proceso', 'listo', 'requiere_revision'}
# Extension permitida -> (tipo MIME, firmas validas al inicio del archivo)
TIPOS_ARCHIVO = {
    'pdf':  ('application/pdf', [b'%PDF']),
    'doc':  ('application/msword', [b'\xd0\xcf\x11\xe0']),
    'docx': ('application/vnd.openxmlformats-officedocument.wordprocessingml.document',
             [b'PK\x03\x04']),
}
MIN_PASSWORD = 8

# ══════════════════════════════════════════
#  BASE DE DATOS
# ══════════════════════════════════════════
DATABASE_URL = os.environ.get('DATABASE_URL')

def get_db():
    """Dentro de una peticion reutiliza una sola conexion, que cerrar_db()
    cierra al final aunque haya un error. Fuera de peticiones (arranque,
    planificador, pruebas) abre una conexion que el llamador cierra."""
    if has_app_context():
        if 'db' not in g or g.db.closed:
            g.db = psycopg2.connect(DATABASE_URL,
                                    cursor_factory=psycopg2.extras.RealDictCursor)
        return g.db
    return psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)

@app.teardown_appcontext
def cerrar_db(error):
    conn = g.pop('db', None)
    if conn is not None and not conn.closed:
        conn.close()

def init_db():
    conn = get_db()
    try:
        _init_db(conn)
    finally:
        conn.close()

def _init_db(conn):
    c = conn.cursor()

    c.execute('''CREATE TABLE IF NOT EXISTS usuarios (
        id SERIAL PRIMARY KEY,
        nombre TEXT NOT NULL,
        email TEXT UNIQUE NOT NULL,
        password TEXT NOT NULL,
        rol TEXT NOT NULL
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS documentos (
        id SERIAL PRIMARY KEY,
        titulo TEXT NOT NULL,
        cliente TEXT NOT NULL,
        archivo TEXT NOT NULL,
        fecha_vencimiento DATE NOT NULL,
        notas TEXT,
        comentario_abogado TEXT,
        estado_caso TEXT DEFAULT 'pendiente',
        abogado_id INTEGER REFERENCES usuarios(id) ON DELETE RESTRICT,
        asignado_por INTEGER,
        fecha_subida TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        fecha_actualizacion TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        alerta_enviada INTEGER DEFAULT 0
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS archivos (
        nombre TEXT PRIMARY KEY,
        tipo TEXT NOT NULL,
        contenido BYTEA NOT NULL,
        fecha_subida TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS intentos_login (
        id SERIAL PRIMARY KEY,
        email TEXT NOT NULL,
        fecha TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    c.execute("CREATE INDEX IF NOT EXISTS idx_intentos_email ON intentos_login (email, fecha)")

    c.execute('''CREATE TABLE IF NOT EXISTS auditoria (
        id SERIAL PRIMARY KEY,
        fecha TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        usuario_id INTEGER,
        usuario_nombre TEXT,
        accion TEXT NOT NULL,
        detalle TEXT
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS tokens_recuperacion (
        id SERIAL PRIMARY KEY,
        usuario_id INTEGER NOT NULL REFERENCES usuarios(id) ON DELETE CASCADE,
        token_hash TEXT UNIQUE NOT NULL,
        creado TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        expira TIMESTAMP NOT NULL,
        usado BOOLEAN DEFAULT FALSE
    )''')
    conn.commit()

    migraciones = [
        "ALTER TABLE documentos ADD COLUMN IF NOT EXISTS comentario_abogado TEXT",
        "ALTER TABLE documentos ADD COLUMN IF NOT EXISTS estado_caso TEXT DEFAULT 'pendiente'",
        "ALTER TABLE documentos ADD COLUMN IF NOT EXISTS fecha_actualizacion TIMESTAMP DEFAULT CURRENT_TIMESTAMP",
        "ALTER TABLE documentos ADD COLUMN IF NOT EXISTS alerta_enviada INTEGER DEFAULT 0",
        "ALTER TABLE documentos ADD COLUMN IF NOT EXISTS alerta_vencido_enviada INTEGER DEFAULT 0",
        # Papelera: un caso eliminado conserva su fila con la fecha de eliminacion
        "ALTER TABLE documentos ADD COLUMN IF NOT EXISTS eliminado_en TIMESTAMP",
        # 'finalizado' no existe en la interfaz: su equivalente es 'listo'
        "UPDATE documentos SET estado_caso = 'listo' WHERE estado_caso = 'finalizado'",
    ]

    # fecha_vencimiento pasa de TEXT a DATE en bases creadas antes
    c.execute("""SELECT data_type FROM information_schema.columns
                 WHERE table_name = 'documentos' AND column_name = 'fecha_vencimiento'""")
    if c.fetchone()['data_type'] != 'date':
        migraciones.append(
            "ALTER TABLE documentos ALTER COLUMN fecha_vencimiento TYPE DATE "
            "USING fecha_vencimiento::date")

    # Llave foranea: no se puede borrar un abogado que tenga casos
    c.execute("SELECT 1 FROM pg_constraint WHERE conname = 'fk_documentos_abogado'")
    if not c.fetchone():
        migraciones.append(
            "ALTER TABLE documentos ADD CONSTRAINT fk_documentos_abogado "
            "FOREIGN KEY (abogado_id) REFERENCES usuarios(id) ON DELETE RESTRICT")

    # Cada migracion se confirma sola: si una falla, no deshace las anteriores
    for sql in migraciones:
        try:
            c.execute(sql)
            conn.commit()
        except psycopg2.Error as e:
            conn.rollback()
            log.warning("Migracion omitida: %s", e)

    # La clave del administrador real nunca va en el codigo: sale de ADMIN_PASSWORD
    admin_password = os.environ.get('ADMIN_PASSWORD')
    if admin_password:
        try:
            c.execute("INSERT INTO usuarios (nombre, email, password, rol) VALUES (%s, %s, %s, %s)",
                ('Super Admin', 'admin@lexdoc.com',
                 generate_password_hash(admin_password), 'superadmin'))
            conn.commit()
        except psycopg2.IntegrityError:
            conn.rollback()

    # ── Usuarios demo para acceso de invitado (solo en modo demo) ──
    usuarios_demo = [] if not MODO_DEMO else [
        ('Super Admin Demo', 'superadmin.demo@lexdoc.com', 'demo123', 'superadmin'),
        ('Jefe Demo', 'jefe.demo@lexdoc.com', 'demo123', 'jefe'),
        ('Abogado Demo', 'abogado.demo@lexdoc.com', 'demo123', 'abogado'),
    ]
    for nombre, email, pw, rol in usuarios_demo:
        try:
            c.execute("INSERT INTO usuarios (nombre, email, password, rol) VALUES (%s, %s, %s, %s)",
                (nombre, email, generate_password_hash(pw), rol))
            conn.commit()
        except psycopg2.IntegrityError:
            conn.rollback()

    conn.commit()

# ══════════════════════════════════════════
#  HELPERS
# ══════════════════════════════════════════
def login_requerido(roles_permitidos):
    def decorador(f):
        from functools import wraps
        @wraps(f)
        def wrapper(*args, **kwargs):
            if 'usuario_id' not in session:
                return redirect(url_for('login'))
            if session.get('rol') not in roles_permitidos:
                flash('No tienes permiso para acceder aquí', 'error')
                return redirect(url_for('dashboard'))
            return f(*args, **kwargs)
        return wrapper
    return decorador

def calcular_estado(fecha_vencimiento):
    """Dias restantes y estado de alerta. Mismos umbrales para todos los roles."""
    vencimiento = datetime.strptime(str(fecha_vencimiento), '%Y-%m-%d').date()
    dias = (vencimiento - datetime.now().date()).days
    if dias < 0:
        estado = 'vencido'
    elif dias <= 7:
        estado = 'urgente'
    elif dias <= 15:
        estado = 'proximo'
    else:
        estado = 'ok'
    return estado, dias

def registrar(c, accion, detalle=''):
    """Deja constancia de quien hizo que. Se confirma junto con la accion."""
    c.execute(
        "INSERT INTO auditoria (usuario_id, usuario_nombre, accion, detalle) "
        "VALUES (%s, %s, %s, %s)",
        (session.get('usuario_id'), session.get('usuario_nombre', 'anonimo'),
         accion, detalle))

def enviar_correo(destinatario, asunto, texto):
    """Envia un correo por Resend. Devuelve True si salio."""
    clave = os.environ.get('RESEND_API_KEY')
    if not clave:
        log.warning("Correo no enviado a %s: falta RESEND_API_KEY", destinatario)
        return False
    resend.api_key = clave
    try:
        resend.Emails.send({
            "from": "onboarding@resend.dev",
            "to": [destinatario],
            "subject": asunto,
            "text": texto,
        })
        log.info("Correo enviado a %s: %s", destinatario, asunto)
        return True
    except Exception as e:  # la libreria puede lanzar errores de red o de API
        log.error("Error enviando correo a %s: %s", destinatario, e)
        return False

def hash_token(token):
    return hashlib.sha256(token.encode()).hexdigest()

def fecha_valida(texto):
    try:
        datetime.strptime(texto, '%Y-%m-%d')
        return True
    except (ValueError, TypeError):
        return False

def es_abogado(c, usuario_id):
    """El id debe ser numerico y pertenecer a un usuario con rol abogado."""
    if not str(usuario_id or '').isdigit():
        return False
    c.execute("SELECT 1 FROM usuarios WHERE id = %s AND rol = 'abogado'", (usuario_id,))
    return c.fetchone() is not None

def guardar_archivo(c, archivo):
    """Valida extension y firma real del archivo, y lo guarda en la base con
    nombre unico. En modo demo solo valida: no guarda archivos de visitantes.
    Devuelve el nombre, o None si el archivo no es valido."""
    if not archivo or not archivo.filename:
        return None
    nombre = secure_filename(archivo.filename)
    extension = nombre.rsplit('.', 1)[-1].lower() if '.' in nombre else ''
    if extension not in TIPOS_ARCHIVO:
        return None
    tipo, firmas = TIPOS_ARCHIVO[extension]
    contenido = archivo.read()
    # La extension se puede cambiar; los primeros bytes del archivo no mienten
    if not any(contenido.startswith(f) for f in firmas):
        return None
    nombre_unico = f"{uuid.uuid4().hex}_{nombre}"
    if not MODO_DEMO:
        c.execute("INSERT INTO archivos (nombre, tipo, contenido) VALUES (%s, %s, %s)",
                  (nombre_unico, tipo, psycopg2.Binary(contenido)))
    return nombre_unico

def bloqueado_en_demo():
    """Sin restricciones. Todo el sistema es una demostracion: cualquier
    visitante puede ejecutar cualquier accion. Los datos se limpian solos."""
    return False


# ══════════════════════════════════════════
#  FRANJA DE AVISO DEMO (todas las paginas)
# ══════════════════════════════════════════
# El aviso de modo demostracion vive en templates/base_app.html y login.html

MESES = ['ene', 'feb', 'mar', 'abr', 'may', 'jun', 'jul', 'ago', 'sep', 'oct', 'nov', 'dic']

@app.template_filter('fecha')
def formato_fecha(valor):
    """12 oct 2026. Acepta date, datetime o texto AAAA-MM-DD."""
    if not valor:
        return ''
    if isinstance(valor, str):
        try:
            valor = datetime.strptime(valor[:10], '%Y-%m-%d')
        except ValueError:
            return valor
    return f"{valor.day} {MESES[valor.month - 1]} {valor.year}"


@app.after_request
def cabeceras_seguridad(response):
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['Referrer-Policy'] = 'same-origin'
    # CSP: solo se ejecuta JavaScript servido por la propia app (static/js).
    # Los estilos en linea se permiten; el riesgo real esta en los scripts.
    if response.content_type.startswith('text/html'):
        response.headers['Content-Security-Policy'] = (
            "default-src 'self'; "
            "script-src 'self'; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            "font-src 'self' https://fonts.gstatic.com; "
            "img-src 'self' data:; "
            "object-src 'none'; base-uri 'self'; "
            "form-action 'self'; frame-ancestors 'none'"
        )
    return response

@app.context_processor
def variables_globales():
    return {'modo_demo': MODO_DEMO}

@app.errorhandler(CSRFError)
def error_csrf(e):
    flash('El formulario expiro. Intenta de nuevo.', 'error')
    return redirect(url_for('dashboard'))

@app.errorhandler(413)
def archivo_muy_grande(e):
    flash('El archivo supera el limite de 10 MB', 'error')
    return redirect(url_for('dashboard'))

# ══════════════════════════════════════════
#  LOGIN / LOGOUT
# ══════════════════════════════════════════
@app.route('/favicon.ico')
def favicon():
    return redirect(url_for('static', filename='favicon.svg'), code=301)

@app.route('/')
def index():
    if 'usuario_id' not in session:
        return redirect(url_for('login'))
    return redirect(url_for('dashboard'))

# Intentos fallidos por email, guardados en PostgreSQL: sobreviven reinicios
# y funcionan aunque haya varios workers.
MAX_INTENTOS = 5
MINUTOS_BLOQUEO = 15

def bloqueado_por_intentos(c, email):
    c.execute(
        "SELECT COUNT(*) AS n FROM intentos_login "
        "WHERE email = %s AND fecha > NOW() - make_interval(mins => %s)",
        (email, MINUTOS_BLOQUEO))
    return c.fetchone()['n'] >= MAX_INTENTOS

def registrar_intento_fallido(conn, email):
    c = conn.cursor()
    c.execute("INSERT INTO intentos_login (email) VALUES (%s)", (email,))
    registrar(c, 'login_fallido', email)
    # Limpieza: los intentos viejos ya no cuentan
    c.execute("DELETE FROM intentos_login WHERE fecha < NOW() - make_interval(mins => %s)",
              (MINUTOS_BLOQUEO,))
    conn.commit()

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')

        conn = get_db()
        c = conn.cursor()
        if bloqueado_por_intentos(c, email):
            flash(f'Demasiados intentos fallidos. Espera {MINUTOS_BLOQUEO} minutos.', 'error')
            return render_template('login.html')

        c.execute("SELECT * FROM usuarios WHERE LOWER(email) = %s", (email,))
        usuario = c.fetchone()

        if usuario and check_password_hash(usuario['password'], password):
            c.execute("DELETE FROM intentos_login WHERE email = %s", (email,))
            conn.commit()
            session.clear()  # sesion nueva: evita fijacion de sesion
            session.permanent = True  # aplica el vencimiento por inactividad
            session['usuario_id'] = usuario['id']
            session['usuario_nombre'] = usuario['nombre']
            session['rol'] = usuario['rol']
            registrar(c, 'login')
            conn.commit()
            return redirect(url_for('dashboard'))
        else:
            registrar_intento_fallido(conn, email)
            flash('Email o contraseña incorrectos', 'error')

    return render_template('login.html')

# ── Acceso rápido como invitado (sin contraseña) ──
DEMO_EMAILS = {
    'superadmin': 'superadmin.demo@lexdoc.com',
    'jefe': 'jefe.demo@lexdoc.com',
    'abogado': 'abogado.demo@lexdoc.com',
}

@app.route('/invitado/<rol>')
def entrar_invitado(rol):
    if not MODO_DEMO:
        abort(404)
    if rol not in DEMO_EMAILS:
        return redirect(url_for('login'))

    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM usuarios WHERE email = %s", (DEMO_EMAILS[rol],))
    usuario = c.fetchone()

    # Si la cuenta no existe todavia, se crea en el momento. Esto evita que
    # el boton falle cuando la base es nueva o quedo incompleta.
    if not usuario:
        nombres = {'superadmin': 'Super Admin Demo',
                   'jefe': 'Jefe Demo',
                   'abogado': 'Abogado Demo'}
        try:
            c.execute(
                "INSERT INTO usuarios (nombre, email, password, rol) VALUES (%s, %s, %s, %s)",
                (nombres[rol], DEMO_EMAILS[rol],
                 generate_password_hash('demo123'), rol)
            )
            conn.commit()
            c.execute("SELECT * FROM usuarios WHERE email = %s", (DEMO_EMAILS[rol],))
            usuario = c.fetchone()
        except psycopg2.Error as e:
            conn.rollback()
            log.error("No se pudo crear la cuenta demo %s: %s", rol, e)

    conn.close()

    if usuario:
        session.clear()
        session.permanent = True
        session['usuario_id'] = usuario['id']
        session['usuario_nombre'] = usuario['nombre']
        session['rol'] = usuario['rol']
        session['demo'] = True
        return redirect(url_for('dashboard'))

    flash('No se pudo iniciar la demostracion. Recarga la pagina.', 'error')
    return redirect(url_for('login'))

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

@app.route('/dashboard')
def dashboard():
    if 'usuario_id' not in session:
        return redirect(url_for('login'))
    rol = session.get('rol')
    if rol == 'superadmin':
        return redirect(url_for('superadmin_dashboard'))
    elif rol == 'jefe':
        return redirect(url_for('jefe_dashboard'))
    elif rol == 'abogado':
        return redirect(url_for('abogado_dashboard'))
    else:
        session.clear()
        return redirect(url_for('login'))

# ══════════════════════════════════════════
#  SUPER ADMIN
# ══════════════════════════════════════════
@app.route('/superadmin')
@login_requerido(['superadmin'])
def superadmin_dashboard():
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM usuarios WHERE rol != 'superadmin'")
    usuarios = c.fetchall()
    c.execute('''SELECT d.*, u.nombre as abogado
               FROM documentos d
               JOIN usuarios u ON d.abogado_id = u.id
               WHERE d.eliminado_en IS NULL''')
    documentos = c.fetchall()
    conn.close()

    docs_con_estado = []
    for doc in documentos:
        estado, dias = calcular_estado(doc['fecha_vencimiento'])
        docs_con_estado.append((doc, estado, dias))

    return render_template('superadmin/dashboard.html',
                         usuarios=usuarios,
                         documentos=docs_con_estado,
                         nombre=session['usuario_nombre'])

@app.route('/superadmin/usuarios')
@login_requerido(['superadmin'])
def superadmin_usuarios():
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM usuarios WHERE rol != 'superadmin'")
    usuarios = c.fetchall()
    conn.close()
    return render_template('superadmin/usuarios.html',
                         usuarios=usuarios,
                         nombre=session['usuario_nombre'])

@app.route('/superadmin/crear_usuario', methods=['POST'])
@login_requerido(['superadmin'])
def crear_usuario():
    if bloqueado_en_demo():
        return redirect(url_for('superadmin_usuarios'))
    nombre = request.form.get('nombre', '').strip()
    email = request.form.get('email', '').strip().lower()
    password = request.form.get('password', '')
    rol = request.form.get('rol', '')

    if not nombre or not email:
        flash('Nombre y email son obligatorios', 'error')
        return redirect(url_for('superadmin_usuarios'))
    if rol not in ROLES_ASIGNABLES:
        flash('Rol no valido', 'error')
        return redirect(url_for('superadmin_usuarios'))
    if len(password) < MIN_PASSWORD:
        flash(f'La contraseña debe tener mínimo {MIN_PASSWORD} caracteres', 'error')
        return redirect(url_for('superadmin_usuarios'))

    conn = get_db()
    c = conn.cursor()
    try:
        c.execute(
            "INSERT INTO usuarios (nombre, email, password, rol) VALUES (%s, %s, %s, %s)",
            (nombre, email, generate_password_hash(password), rol)
        )
        registrar(c, 'crear_usuario', f'{email} ({rol})')
        conn.commit()
        flash(f'Usuario {nombre} creado correctamente', 'success')
    except psycopg2.IntegrityError:
        conn.rollback()
        flash('El email ya está registrado', 'error')
    return redirect(url_for('superadmin_usuarios'))

@app.route('/superadmin/eliminar_usuario/<int:id>', methods=['POST'])
@login_requerido(['superadmin'])
def eliminar_usuario(id):
    if bloqueado_en_demo():
        return redirect(url_for('superadmin_usuarios'))
    conn = get_db()
    c = conn.cursor()
    try:
        # Un superadmin nunca se borra desde aqui, ni siquiera a si mismo
        c.execute("DELETE FROM usuarios WHERE id = %s AND rol != 'superadmin' RETURNING email", (id,))
        borrado = c.fetchone()
        if borrado:
            registrar(c, 'eliminar_usuario', borrado['email'])
        conn.commit()
        if borrado:
            flash('Usuario eliminado', 'success')
        else:
            flash('Usuario no encontrado', 'error')
    except psycopg2.IntegrityError:
        conn.rollback()
        flash('No se puede eliminar: tiene casos asignados (tambien cuentan los de la papelera).', 'error')
    return redirect(url_for('superadmin_usuarios'))

@app.route('/superadmin/editar_usuario/<int:id>', methods=['GET', 'POST'])
@login_requerido(['superadmin'])
def editar_usuario(id):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM usuarios WHERE id = %s AND rol != 'superadmin'", (id,))
    usuario = c.fetchone()
    if not usuario:
        flash('Usuario no encontrado', 'error')
        return redirect(url_for('superadmin_usuarios'))

    if request.method == 'POST':
        if bloqueado_en_demo():
            return redirect(url_for('superadmin_usuarios'))
        nombre = request.form.get('nombre', '').strip()
        email = request.form.get('email', '').strip().lower()
        rol = request.form.get('rol', '')
        nueva_password = request.form.get('password', '')

        if not nombre or not email:
            flash('Nombre y email son obligatorios', 'error')
            return redirect(url_for('editar_usuario', id=id))
        if rol not in ROLES_ASIGNABLES:
            flash('Rol no valido', 'error')
            return redirect(url_for('editar_usuario', id=id))
        if nueva_password and len(nueva_password) < MIN_PASSWORD:
            flash(f'La contraseña debe tener mínimo {MIN_PASSWORD} caracteres', 'error')
            return redirect(url_for('editar_usuario', id=id))

        # Un abogado con casos no puede dejar de ser abogado
        if usuario['rol'] == 'abogado' and rol != 'abogado':
            c.execute("SELECT 1 FROM documentos WHERE abogado_id = %s LIMIT 1", (id,))
            if c.fetchone():
                flash('Tiene casos asignados. Reasignalos antes de cambiar el rol.', 'error')
                return redirect(url_for('editar_usuario', id=id))

        try:
            if nueva_password:
                c.execute(
                    "UPDATE usuarios SET nombre=%s, email=%s, rol=%s, password=%s WHERE id=%s",
                    (nombre, email, rol, generate_password_hash(nueva_password), id)
                )
            else:
                c.execute(
                    "UPDATE usuarios SET nombre=%s, email=%s, rol=%s WHERE id=%s",
                    (nombre, email, rol, id)
                )
            registrar(c, 'editar_usuario',
                      f'{email} ({rol})' + (' + cambio de contraseña' if nueva_password else ''))
            conn.commit()
            flash('Usuario actualizado correctamente', 'success')
        except psycopg2.IntegrityError:
            conn.rollback()
            flash('El email ya está en uso', 'error')
            return redirect(url_for('editar_usuario', id=id))
        return redirect(url_for('superadmin_usuarios'))

    return render_template('superadmin/editar_usuario.html',
                         usuario=usuario,
                         nombre=session['usuario_nombre'])

@app.route('/superadmin/perfil', methods=['GET', 'POST'])
@login_requerido(['superadmin'])
def superadmin_perfil():
    conn = get_db()
    c = conn.cursor()
    if request.method == 'POST':
        if bloqueado_en_demo():
            conn.close()
            return redirect(url_for('superadmin_perfil'))
        nombre = request.form.get('nombre', '').strip()
        email = request.form.get('email', '').strip().lower()
        if not nombre or not email:
            flash('Nombre y email son obligatorios', 'error')
            return redirect(url_for('superadmin_perfil'))
        try:
            c.execute(
                "UPDATE usuarios SET nombre=%s, email=%s WHERE id=%s",
                (nombre, email, session['usuario_id'])
            )
            registrar(c, 'editar_perfil', email)
            conn.commit()
            session['usuario_nombre'] = nombre
            flash('Perfil actualizado correctamente', 'success')
        except psycopg2.IntegrityError:
            conn.rollback()
            flash('El email ya está en uso', 'error')
        conn.close()
        return redirect(url_for('superadmin_perfil'))

    c.execute("SELECT * FROM usuarios WHERE id = %s", (session['usuario_id'],))
    admin = c.fetchone()
    conn.close()
    return render_template('superadmin/perfil.html',
                         admin=admin,
                         nombre=session['usuario_nombre'])

@app.route('/superadmin/cambiar_password', methods=['GET', 'POST'])
@login_requerido(['superadmin'])
def cambiar_password():
    if request.method == 'POST':
        if bloqueado_en_demo():
            return redirect(url_for('cambiar_password'))
        password_actual = request.form.get('password_actual', '')
        password_nueva = request.form.get('password_nueva', '')
        password_confirmar = request.form.get('password_confirmar', '')

        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT * FROM usuarios WHERE id = %s", (session['usuario_id'],))
        admin = c.fetchone()

        if not check_password_hash(admin['password'], password_actual):
            flash('La contraseña actual es incorrecta', 'error')
            conn.close()
            return redirect(url_for('cambiar_password'))

        if password_nueva != password_confirmar:
            flash('Las contraseñas nuevas no coinciden', 'error')
            conn.close()
            return redirect(url_for('cambiar_password'))

        if len(password_nueva) < MIN_PASSWORD:
            flash(f'La contraseña debe tener mínimo {MIN_PASSWORD} caracteres', 'error')
            conn.close()
            return redirect(url_for('cambiar_password'))

        c.execute(
            "UPDATE usuarios SET password = %s WHERE id = %s",
            (generate_password_hash(password_nueva), session['usuario_id'])
        )
        registrar(c, 'cambiar_password')
        conn.commit()
        conn.close()
        flash('Contraseña actualizada correctamente', 'success')
        return redirect(url_for('superadmin_dashboard'))

    return render_template('superadmin/cambiar_password.html',
                         nombre=session['usuario_nombre'])

# ══════════════════════════════════════════
#  JEFE DE FIRMA
# ══════════════════════════════════════════
@app.route('/jefe')
@login_requerido(['jefe'])
def jefe_dashboard():
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM usuarios WHERE rol = 'abogado'")
    abogados = c.fetchall()
    c.execute('''SELECT d.*, u.nombre as abogado FROM documentos d
               JOIN usuarios u ON d.abogado_id = u.id
               WHERE d.eliminado_en IS NULL
               ORDER BY d.fecha_vencimiento ASC''')
    documentos = c.fetchall()
    conn.close()

    docs_con_estado = []
    for doc in documentos:
        estado, dias = calcular_estado(doc['fecha_vencimiento'])
        docs_con_estado.append((doc, estado, dias))

    return render_template('jefe/dashboard.html',
                         abogados=abogados,
                         documentos=docs_con_estado,
                         nombre=session['usuario_nombre'])

@app.route('/jefe/asignar', methods=['GET', 'POST'])
@login_requerido(['jefe'])
def jefe_asignar():
    conn = get_db()
    c = conn.cursor()
    if request.method == 'POST':
        if bloqueado_en_demo():
            conn.close()
            return redirect(url_for('jefe_dashboard'))
        abogado_id = request.form.get('abogado_id')
        if not abogado_id:
            flash('Debes seleccionar un abogado', 'error')
            c.execute("SELECT * FROM usuarios WHERE rol = 'abogado'")
            abogados = c.fetchall()
            conn.close()
            return render_template('jefe/asignar.html',
                                 abogados=abogados,
                                 nombre=session['usuario_nombre'])

        titulo = request.form.get('titulo', '').strip()
        cliente = request.form.get('cliente', '').strip()
        fecha_vencimiento = request.form.get('fecha_vencimiento', '')
        notas = request.form.get('notas', '')

        if not es_abogado(c, abogado_id):
            flash('El abogado seleccionado no es valido', 'error')
            return redirect(url_for('jefe_asignar'))
        if not titulo or not cliente or not fecha_valida(fecha_vencimiento):
            flash('Titulo, cliente y una fecha valida son obligatorios', 'error')
            return redirect(url_for('jefe_asignar'))
        nombre_archivo = guardar_archivo(c, request.files.get('archivo'))
        if not nombre_archivo:
            flash('Archivo no valido. Solo PDF, DOC o DOCX reales.', 'error')
            return redirect(url_for('jefe_asignar'))

        c.execute(
            '''INSERT INTO documentos
               (titulo, cliente, archivo, fecha_vencimiento, notas,
                estado_caso, abogado_id, asignado_por)
               VALUES (%s, %s, %s, %s, %s, 'pendiente', %s, %s)''',
            (titulo, cliente, nombre_archivo, fecha_vencimiento,
             notas, abogado_id, session['usuario_id'])
        )
        registrar(c, 'asignar_caso', f'{titulo} / {cliente}')
        conn.commit()
        conn.close()
        flash('Caso asignado correctamente', 'success')
        return redirect(url_for('jefe_dashboard'))

    c.execute("SELECT * FROM usuarios WHERE rol = 'abogado'")
    abogados = c.fetchall()
    conn.close()
    return render_template('jefe/asignar.html',
                         abogados=abogados,
                         nombre=session['usuario_nombre'])

@app.route('/jefe/editar/<int:id>', methods=['GET', 'POST'])
@login_requerido(['jefe'])
def jefe_editar(id):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM documentos WHERE id = %s AND eliminado_en IS NULL", (id,))
    doc = c.fetchone()

    if not doc:
        flash('Documento no encontrado', 'error')
        return redirect(url_for('jefe_dashboard'))

    if request.method == 'POST':
        if bloqueado_en_demo():
            conn.close()
            return redirect(url_for('jefe_dashboard'))
        titulo = request.form.get('titulo', '').strip()
        cliente = request.form.get('cliente', '').strip()
        fecha_vencimiento = request.form.get('fecha_vencimiento', '')
        notas = request.form.get('notas', '')
        abogado_id = request.form.get('abogado_id')

        if not es_abogado(c, abogado_id):
            flash('El abogado seleccionado no es valido', 'error')
            return redirect(url_for('jefe_editar', id=id))
        if not titulo or not cliente or not fecha_valida(fecha_vencimiento):
            flash('Titulo, cliente y una fecha valida son obligatorios', 'error')
            return redirect(url_for('jefe_editar', id=id))

        c.execute(
            '''UPDATE documentos SET titulo=%s, cliente=%s,
               fecha_vencimiento=%s, notas=%s, abogado_id=%s,
               fecha_actualizacion=%s,
               alerta_enviada = CASE WHEN fecha_vencimiento = %s::date
                                     THEN alerta_enviada ELSE 0 END,
               alerta_vencido_enviada = CASE WHEN fecha_vencimiento = %s::date
                                     THEN alerta_vencido_enviada ELSE 0 END
               WHERE id=%s''',
            (titulo, cliente, fecha_vencimiento, notas,
             abogado_id, datetime.now(), fecha_vencimiento, fecha_vencimiento, id)
        )
        registrar(c, 'editar_caso', f'#{id} {titulo} / {cliente}')
        conn.commit()
        conn.close()
        flash('Caso actualizado correctamente', 'success')
        return redirect(url_for('jefe_dashboard'))

    c.execute("SELECT * FROM usuarios WHERE rol = 'abogado'")
    abogados = c.fetchall()
    conn.close()
    return render_template('jefe/editar.html',
                         doc=doc,
                         abogados=abogados,
                         nombre=session['usuario_nombre'])

@app.route('/jefe/eliminar/<int:id>', methods=['POST'])
@login_requerido(['jefe'])
def jefe_eliminar(id):
    """Envia el caso a la papelera. Se puede restaurar."""
    if bloqueado_en_demo():
        return redirect(url_for('jefe_dashboard'))
    conn = get_db()
    c = conn.cursor()
    c.execute("UPDATE documentos SET eliminado_en = NOW() "
              "WHERE id = %s AND eliminado_en IS NULL RETURNING titulo", (id,))
    caso = c.fetchone()
    if caso:
        registrar(c, 'caso_a_papelera', f"#{id} {caso['titulo']}")
        flash('Caso enviado a la papelera', 'success')
    conn.commit()
    return redirect(url_for('jefe_dashboard'))

@app.route('/jefe/papelera')
@login_requerido(['jefe'])
def jefe_papelera():
    conn = get_db()
    c = conn.cursor()
    c.execute('''SELECT d.*, u.nombre AS abogado FROM documentos d
                 JOIN usuarios u ON d.abogado_id = u.id
                 WHERE d.eliminado_en IS NOT NULL
                 ORDER BY d.eliminado_en DESC''')
    documentos = c.fetchall()
    return render_template('jefe/papelera.html',
                           documentos=documentos,
                           nombre=session['usuario_nombre'])

@app.route('/jefe/restaurar/<int:id>', methods=['POST'])
@login_requerido(['jefe'])
def jefe_restaurar(id):
    conn = get_db()
    c = conn.cursor()
    c.execute("UPDATE documentos SET eliminado_en = NULL "
              "WHERE id = %s AND eliminado_en IS NOT NULL RETURNING titulo", (id,))
    caso = c.fetchone()
    if caso:
        registrar(c, 'restaurar_caso', f"#{id} {caso['titulo']}")
        flash('Caso restaurado', 'success')
    conn.commit()
    return redirect(url_for('jefe_papelera'))

@app.route('/jefe/eliminar_definitivo/<int:id>', methods=['POST'])
@login_requerido(['jefe'])
def jefe_eliminar_definitivo(id):
    """Solo se puede borrar para siempre lo que ya esta en la papelera."""
    conn = get_db()
    c = conn.cursor()
    c.execute("DELETE FROM documentos WHERE id = %s AND eliminado_en IS NOT NULL "
              "RETURNING titulo, archivo", (id,))
    caso = c.fetchone()
    if caso:
        c.execute("DELETE FROM archivos WHERE nombre = %s", (caso['archivo'],))
        registrar(c, 'eliminar_caso_definitivo', f"#{id} {caso['titulo']}")
        flash('Caso eliminado definitivamente', 'success')
    conn.commit()
    return redirect(url_for('jefe_papelera'))

# ══════════════════════════════════════════
#  ABOGADO
# ══════════════════════════════════════════
@app.route('/abogado')
@login_requerido(['abogado'])
def abogado_dashboard():
    conn = get_db()
    c = conn.cursor()
    c.execute(
        '''SELECT * FROM documentos WHERE abogado_id = %s AND eliminado_en IS NULL
           ORDER BY fecha_vencimiento ASC''',
        (session['usuario_id'],)
    )
    documentos = c.fetchall()
    conn.close()

    docs_con_estado = []
    for doc in documentos:
        estado, dias = calcular_estado(doc['fecha_vencimiento'])
        docs_con_estado.append((doc, estado, dias))

    return render_template('abogado/dashboard.html',
                         documentos=docs_con_estado,
                         nombre=session['usuario_nombre'])

@app.route('/abogado/subir', methods=['GET', 'POST'])
@login_requerido(['abogado'])
def abogado_subir():
    if request.method == 'POST':
        if bloqueado_en_demo():
            return redirect(url_for('abogado_dashboard'))
        titulo = request.form.get('titulo', '').strip()
        cliente = request.form.get('cliente', '').strip()
        fecha_vencimiento = request.form.get('fecha_vencimiento', '')
        notas = request.form.get('notas', '')
        comentario = request.form.get('comentario_abogado', '')

        if not titulo or not cliente or not fecha_valida(fecha_vencimiento):
            flash('Titulo, cliente y una fecha valida son obligatorios', 'error')
            return redirect(url_for('abogado_subir'))

        conn = get_db()
        c = conn.cursor()
        nombre_archivo = guardar_archivo(c, request.files.get('archivo'))
        if not nombre_archivo:
            flash('Archivo no valido. Solo PDF, DOC o DOCX reales.', 'error')
            return redirect(url_for('abogado_subir'))
        c.execute(
            '''INSERT INTO documentos
               (titulo, cliente, archivo, fecha_vencimiento, notas,
                comentario_abogado, estado_caso, abogado_id, asignado_por)
               VALUES (%s, %s, %s, %s, %s, %s, 'en_proceso', %s, %s)''',
            (titulo, cliente, nombre_archivo, fecha_vencimiento,
             notas, comentario, session['usuario_id'], session['usuario_id'])
        )
        registrar(c, 'subir_caso', f'{titulo} / {cliente}')
        conn.commit()
        conn.close()
        flash('Documento subido correctamente', 'success')
        return redirect(url_for('abogado_dashboard'))

    return render_template('abogado/subir.html',
                         nombre=session['usuario_nombre'])

@app.route('/abogado/editar/<int:id>', methods=['GET', 'POST'])
@login_requerido(['abogado'])
def abogado_editar(id):
    conn = get_db()
    c = conn.cursor()
    c.execute(
        "SELECT * FROM documentos WHERE id = %s AND abogado_id = %s AND eliminado_en IS NULL",
        (id, session['usuario_id'])
    )
    doc = c.fetchone()

    if not doc:
        flash('Documento no encontrado', 'error')
        return redirect(url_for('abogado_dashboard'))

    if request.method == 'POST':
        if bloqueado_en_demo():
            conn.close()
            return redirect(url_for('abogado_dashboard'))
        titulo = request.form.get('titulo', '').strip()
        cliente = request.form.get('cliente', '').strip()
        fecha_vencimiento = request.form.get('fecha_vencimiento', '')
        comentario = request.form.get('comentario_abogado', '')
        estado_caso = request.form.get('estado_caso', 'pendiente')

        if estado_caso not in ESTADOS_CASO:
            flash('Estado no valido', 'error')
            return redirect(url_for('abogado_editar', id=id))
        if not titulo or not cliente or not fecha_valida(fecha_vencimiento):
            flash('Titulo, cliente y una fecha valida son obligatorios', 'error')
            return redirect(url_for('abogado_editar', id=id))

        c.execute(
            '''UPDATE documentos SET titulo=%s, cliente=%s,
               fecha_vencimiento=%s, comentario_abogado=%s,
               estado_caso=%s, fecha_actualizacion=%s,
               alerta_enviada = CASE WHEN fecha_vencimiento = %s::date
                                     THEN alerta_enviada ELSE 0 END,
               alerta_vencido_enviada = CASE WHEN fecha_vencimiento = %s::date
                                     THEN alerta_vencido_enviada ELSE 0 END
               WHERE id=%s''',
            (titulo, cliente, fecha_vencimiento, comentario, estado_caso,
             datetime.now(), fecha_vencimiento, fecha_vencimiento, id)
        )
        registrar(c, 'editar_caso', f'#{id} {titulo} ({estado_caso})')
        conn.commit()
        conn.close()
        flash('Documento actualizado correctamente', 'success')
        return redirect(url_for('abogado_dashboard'))

    conn.close()
    return render_template('abogado/editar.html',
                         doc=doc,
                         nombre=session['usuario_nombre'])

# ══════════════════════════════════════════
#  DESCARGA DE ARCHIVOS
# ══════════════════════════════════════════
def generar_pdf_prueba(titulo, cliente):
    """Construye un PDF de una pagina que deja claro que es un archivo
    de prueba. No usa librerias externas: arma los bytes del PDF a mano."""
    lineas = [
        (72, 700, 20, 'DOCUMENTO DE PRUEBA'),
        (72, 660, 12, 'Este archivo no contiene informacion real.'),
        (72, 640, 12, 'Fue generado automaticamente por la demostracion de LexDoc.'),
        (72, 590, 12, 'Caso: ' + titulo),
        (72, 570, 12, 'Cliente: ' + cliente),
        (72, 520, 10, 'Ningun dato de esta pantalla corresponde a un caso,'),
        (72, 505, 10, 'cliente o documento legal real.'),
    ]
    partes = []
    for x, y, tam, texto in lineas:
        t = texto.replace('\\', '').replace('(', '').replace(')', '')
        fuente = 'F2' if tam >= 20 else 'F1'
        partes.append('BT /%s %d Tf %d %d Td (%s) Tj ET' % (fuente, tam, x, y, t))
    contenido = '\n'.join(partes).encode('latin-1', 'replace')

    objetos = [
        b'<< /Type /Catalog /Pages 2 0 R >>',
        b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
        b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] '
        b'/Resources << /Font << /F1 4 0 R /F2 5 0 R >> >> /Contents 6 0 R >>',
        b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
        b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>',
        b'<< /Length ' + str(len(contenido)).encode() + b' >>\nstream\n'
        + contenido + b'\nendstream',
    ]

    salida = bytearray(b'%PDF-1.4\n')
    offsets = []
    for i, cuerpo in enumerate(objetos, start=1):
        offsets.append(len(salida))
        salida += str(i).encode() + b' 0 obj\n' + cuerpo + b'\nendobj\n'

    inicio_xref = len(salida)
    salida += b'xref\n0 ' + str(len(objetos) + 1).encode() + b'\n'
    salida += b'0000000000 65535 f \n'
    for off in offsets:
        salida += ('%010d 00000 n \n' % off).encode()
    salida += b'trailer\n<< /Size ' + str(len(objetos) + 1).encode() + b' /Root 1 0 R >>\n'
    salida += b'startxref\n' + str(inicio_xref).encode() + b'\n%%EOF\n'
    return bytes(salida)


@app.route('/descargar/<nombre_archivo>')
@login_requerido(['superadmin', 'jefe', 'abogado'])
def descargar(nombre_archivo):
    """Un abogado solo puede abrir documentos de sus propios casos.
    En demo nunca se entrega un archivo real: se genera un PDF de prueba."""
    conn = get_db()
    c = conn.cursor()
    if session.get('rol') == 'abogado':
        c.execute("SELECT titulo, cliente FROM documentos WHERE archivo = %s AND abogado_id = %s "
                  "AND eliminado_en IS NULL LIMIT 1",
                  (nombre_archivo, session['usuario_id']))
    else:
        c.execute("SELECT titulo, cliente FROM documentos WHERE archivo = %s "
                  "AND eliminado_en IS NULL LIMIT 1",
                  (nombre_archivo,))
    fila = c.fetchone()
    if not fila:
        abort(404)
    registrar(c, 'descargar', f"{fila['titulo']} / {fila['cliente']}")
    conn.commit()

    if MODO_DEMO:
        pdf = generar_pdf_prueba(fila['titulo'], fila['cliente'])
        return Response(
            pdf,
            mimetype='application/pdf',
            headers={'Content-Disposition': 'inline; filename="documento-de-prueba.pdf"'}
        )

    c.execute("SELECT tipo, contenido FROM archivos WHERE nombre = %s", (nombre_archivo,))
    archivo = c.fetchone()
    if not archivo:
        abort(404)
    nombre_original = nombre_archivo.split('_', 1)[-1]
    # attachment: se descarga, el navegador no lo abre dentro de la app
    return Response(
        bytes(archivo['contenido']),
        mimetype=archivo['tipo'],
        headers={'Content-Disposition': f'attachment; filename="{nombre_original}"'}
    )

# ══════════════════════════════════════════
#  RECUPERAR CONTRASEÑA
# ══════════════════════════════════════════
MINUTOS_TOKEN = 60
MAX_SOLICITUDES_HORA = 3

@app.route('/recuperar', methods=['GET', 'POST'])
def recuperar():
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT id, nombre, email FROM usuarios WHERE LOWER(email) = %s", (email,))
        usuario = c.fetchone()
        if usuario:
            c.execute("SELECT COUNT(*) AS n FROM tokens_recuperacion "
                      "WHERE usuario_id = %s AND creado > NOW() - INTERVAL '1 hour'",
                      (usuario['id'],))
            if c.fetchone()['n'] < MAX_SOLICITUDES_HORA:
                token = secrets.token_urlsafe(32)
                # En la base solo queda el hash: si alguien lee la tabla, no puede usar el enlace
                c.execute("INSERT INTO tokens_recuperacion (usuario_id, token_hash, expira) "
                          "VALUES (%s, %s, NOW() + make_interval(mins => %s))",
                          (usuario['id'], hash_token(token), MINUTOS_TOKEN))
                c.execute("INSERT INTO auditoria (usuario_id, usuario_nombre, accion) "
                          "VALUES (%s, %s, 'solicitar_recuperacion')",
                          (usuario['id'], usuario['nombre']))
                conn.commit()
                enlace = f"{APP_URL}{url_for('restablecer', token=token)}"
                enviar_correo(usuario['email'], 'Restablecer contraseña de LexDoc',
                              f"Hola {usuario['nombre']},\n\nPara crear una contraseña nueva "
                              f"entra aqui (valido por {MINUTOS_TOKEN} minutos, un solo uso):\n"
                              f"{enlace}\n\nSi no lo pediste, ignora este correo.\n\n— Sistema LexDoc")
        # Mismo mensaje exista o no el correo: no revela quien tiene cuenta
        flash('Si el correo esta registrado, te enviamos un enlace para restablecer la contraseña.',
              'success')
        return redirect(url_for('login'))
    return render_template('recuperar.html')

@app.route('/restablecer/<token>', methods=['GET', 'POST'])
def restablecer(token):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT t.id, t.usuario_id, u.nombre FROM tokens_recuperacion t "
              "JOIN usuarios u ON u.id = t.usuario_id "
              "WHERE t.token_hash = %s AND NOT t.usado AND t.expira > NOW()",
              (hash_token(token),))
    registro = c.fetchone()
    if not registro:
        flash('El enlace no es valido o ya vencio. Solicita uno nuevo.', 'error')
        return redirect(url_for('recuperar'))

    if request.method == 'POST':
        nueva = request.form.get('password_nueva', '')
        confirmar = request.form.get('password_confirmar', '')
        if nueva != confirmar:
            flash('Las contraseñas no coinciden', 'error')
            return redirect(url_for('restablecer', token=token))
        if len(nueva) < MIN_PASSWORD:
            flash(f'La contraseña debe tener mínimo {MIN_PASSWORD} caracteres', 'error')
            return redirect(url_for('restablecer', token=token))
        c.execute("UPDATE usuarios SET password = %s WHERE id = %s",
                  (generate_password_hash(nueva), registro['usuario_id']))
        # Se invalidan todos los enlaces pendientes de ese usuario
        c.execute("UPDATE tokens_recuperacion SET usado = TRUE WHERE usuario_id = %s",
                  (registro['usuario_id'],))
        c.execute("INSERT INTO auditoria (usuario_id, usuario_nombre, accion) "
                  "VALUES (%s, %s, 'restablecer_password')",
                  (registro['usuario_id'], registro['nombre']))
        conn.commit()
        flash('Contraseña actualizada. Ya puedes iniciar sesion.', 'success')
        return redirect(url_for('login'))

    return render_template('restablecer.html', token=token)

# ══════════════════════════════════════════
#  AUDITORIA
# ══════════════════════════════════════════
@app.route('/superadmin/auditoria')
@login_requerido(['superadmin'])
def superadmin_auditoria():
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM auditoria ORDER BY fecha DESC LIMIT 300")
    registros = c.fetchall()
    return render_template('superadmin/auditoria.html',
                           registros=registros,
                           nombre=session['usuario_nombre'])

@app.route('/reset-alertas', methods=['POST'])
@login_requerido(['superadmin'])
def reset_alertas():
    conn = get_db()
    c = conn.cursor()
    c.execute('UPDATE documentos SET alerta_enviada = 0, alerta_vencido_enviada = 0')
    registrar(c, 'reiniciar_alertas')
    conn.commit()
    conn.close()
    flash('Alertas reiniciadas correctamente', 'success')
    return redirect(url_for('superadmin_dashboard'))

# ══════════════════════════════════════════
#  ALERTAS POR EMAIL
# ══════════════════════════════════════════
def enviar_alertas():
    conn = get_db()
    try:
        _enviar_alertas(conn)
    finally:
        conn.close()

def _enviar_alertas(conn):
    """Dos avisos por caso: uno cuando faltan 7 dias o menos, y otro cuando
    ya vencio. Los casos listos o en la papelera no generan alertas."""
    c = conn.cursor()
    hoy = datetime.now().date()
    limite = hoy + timedelta(days=7)
    base = '''SELECT d.id, d.titulo, d.cliente, d.fecha_vencimiento, u.email, u.nombre
              FROM documentos d JOIN usuarios u ON d.abogado_id = u.id
              WHERE d.eliminado_en IS NULL AND d.estado_caso != 'listo' AND '''

    c.execute(base + "d.fecha_vencimiento BETWEEN %s AND %s AND d.alerta_enviada = 0",
              (hoy, limite))
    for doc in c.fetchall():
        texto = (f"Hola {doc['nombre']},\n\nEl documento '{doc['titulo']}' del cliente "
                 f"{doc['cliente']} vence el {doc['fecha_vencimiento']}.\n\n"
                 "Por favor toma las acciones necesarias.\n\n— Sistema LexDoc")
        if enviar_correo(doc['email'], f"Documento por vencer: {doc['titulo']}", texto):
            c.execute("UPDATE documentos SET alerta_enviada = 1 WHERE id = %s", (doc['id'],))
            conn.commit()

    c.execute(base + "d.fecha_vencimiento < %s AND d.alerta_vencido_enviada = 0", (hoy,))
    for doc in c.fetchall():
        texto = (f"Hola {doc['nombre']},\n\nEl documento '{doc['titulo']}' del cliente "
                 f"{doc['cliente']} VENCIO el {doc['fecha_vencimiento']} y no esta marcado "
                 "como listo.\n\nRevisa el caso de inmediato.\n\n— Sistema LexDoc")
        if enviar_correo(doc['email'], f"VENCIDO: {doc['titulo']}", texto):
            c.execute("UPDATE documentos SET alerta_vencido_enviada = 1 WHERE id = %s",
                      (doc['id'],))
            conn.commit()

# ══════════════════════════════════════════
#  DATOS DE DEMOSTRACION
# ══════════════════════════════════════════
def sembrar_documentos_demo():
    """Carga casos de ejemplo si la base esta vacia."""
    conn = get_db()
    try:
        _sembrar_documentos_demo(conn)
    finally:
        conn.close()

def _sembrar_documentos_demo(conn):
    c = conn.cursor()

    c.execute("SELECT COUNT(*) AS n FROM documentos")
    if c.fetchone()['n'] > 0:
        return

    c.execute("SELECT id FROM usuarios WHERE email = %s", ('abogado.demo@lexdoc.com',))
    f = c.fetchone()
    abogado_id = f['id'] if f else None
    c.execute("SELECT id FROM usuarios WHERE email = %s", ('jefe.demo@lexdoc.com',))
    f = c.fetchone()
    jefe_id = f['id'] if f else None

    hoy = datetime.now()
    ejemplos = [
        ('PRUEBA - Contrato de arrendamiento', 'CLIENTE PRUEBA 01',
         'prueba-contrato.pdf', (hoy + timedelta(days=12)).strftime('%Y-%m-%d'),
         'Dato de prueba. No corresponde a ningun caso real.', 'pendiente'),
        ('PRUEBA - Poder general', 'CLIENTE PRUEBA 02',
         'prueba-poder.pdf', (hoy + timedelta(days=45)).strftime('%Y-%m-%d'),
         'Dato de prueba. No corresponde a ningun caso real.', 'en_proceso'),
        ('PRUEBA - Demanda laboral', 'CLIENTE PRUEBA 03',
         'prueba-demanda.pdf', (hoy + timedelta(days=5)).strftime('%Y-%m-%d'),
         'Dato de prueba. No corresponde a ningun caso real.', 'en_proceso'),
        ('PRUEBA - Acta de conciliacion', 'CLIENTE PRUEBA 04',
         'prueba-acta.pdf', (hoy + timedelta(days=90)).strftime('%Y-%m-%d'),
         'Dato de prueba. No corresponde a ningun caso real.', 'listo'),
        ('PRUEBA - Escritura de compraventa', 'CLIENTE PRUEBA 05',
         'prueba-escritura.pdf', (hoy + timedelta(days=25)).strftime('%Y-%m-%d'),
         'Dato de prueba. No corresponde a ningun caso real.', 'pendiente'),
    ]
    for titulo, cliente, archivo, venc, notas, estado in ejemplos:
        try:
            c.execute("""INSERT INTO documentos
                (titulo, cliente, archivo, fecha_vencimiento, notas,
                 estado_caso, abogado_id, asignado_por)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                (titulo, cliente, archivo, venc, notas, estado, abogado_id, jefe_id))
        except Exception:
            conn.rollback()
    conn.commit()


def resetear_demo():
    """Devuelve la demostracion a su estado inicial."""
    conn = get_db()
    c = conn.cursor()
    try:
        c.execute("DELETE FROM documentos")
        c.execute("DELETE FROM archivos")
        c.execute("DELETE FROM intentos_login")
        c.execute("DELETE FROM tokens_recuperacion")
        c.execute("DELETE FROM auditoria")
        c.execute("DELETE FROM usuarios")
        conn.commit()
    except psycopg2.Error as e:
        conn.rollback()
        log.error("Error al limpiar la demo: %s", e)
        return
    finally:
        conn.close()

    init_db()
    sembrar_documentos_demo()
    log.info("Demostracion reiniciada")


# ══════════════════════════════════════════
#  INICIAR
# ══════════════════════════════════════════
init_db()
if MODO_DEMO:
    sembrar_documentos_demo()


if __name__ == '__main__':
    # El modo debug solo se activa a proposito: FLASK_DEBUG=1
    app.run(debug=os.environ.get('FLASK_DEBUG') == '1')
