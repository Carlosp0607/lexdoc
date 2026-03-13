from flask import Flask, render_template, request, redirect, url_for, session, flash, send_from_directory
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from apscheduler.schedulers.background import BackgroundScheduler
import sqlite3
import os
import smtplib
from email.mime.text import MIMEText
from datetime import datetime, timedelta

app = Flask(__name__)
app.secret_key = 'lexdoc-clave-secreta-2024'
UPLOAD_FOLDER = 'uploads'
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

# ══════════════════════════════════════════
#  BASE DE DATOS
# ══════════════════════════════════════════
def get_db():
    conn = sqlite3.connect('lexdoc.db')
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    c = conn.cursor()

    c.execute('''CREATE TABLE IF NOT EXISTS usuarios (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        nombre TEXT NOT NULL,
        email TEXT UNIQUE NOT NULL,
        password TEXT NOT NULL,
        rol TEXT NOT NULL
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS documentos (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        titulo TEXT NOT NULL,
        cliente TEXT NOT NULL,
        archivo TEXT NOT NULL,
        fecha_vencimiento TEXT NOT NULL,
        notas TEXT,
        comentario_abogado TEXT,
        estado_caso TEXT DEFAULT 'pendiente',
        abogado_id INTEGER,
        asignado_por INTEGER,
        fecha_subida TEXT DEFAULT CURRENT_TIMESTAMP,
        fecha_actualizacion TEXT DEFAULT CURRENT_TIMESTAMP,
        alerta_enviada INTEGER DEFAULT 0,
        FOREIGN KEY (abogado_id) REFERENCES usuarios(id),
        FOREIGN KEY (asignado_por) REFERENCES usuarios(id)
    )''')

    # ── Migraciones automáticas ──────────────────────────
    # Agrega columnas nuevas si no existen, sin borrar datos
    columnas_documentos = [
        ("comentario_abogado", "TEXT"),
        ("estado_caso",        "TEXT DEFAULT 'pendiente'"),
        ("fecha_actualizacion","TEXT DEFAULT CURRENT_TIMESTAMP"),
        ("alerta_enviada",     "INTEGER DEFAULT 0"),
    ]
    for col, tipo in columnas_documentos:
        try:
            c.execute(f"ALTER TABLE documentos ADD COLUMN {col} {tipo}")
            print(f"✅ Columna '{col}' agregada a documentos")
        except:
            pass  # Ya existe, no hace nada

    columnas_usuarios = [
        # Aquí agregas futuras columnas de usuarios
    ]
    for col, tipo in columnas_usuarios:
        try:
            c.execute(f"ALTER TABLE usuarios ADD COLUMN {col} {tipo}")
            print(f"✅ Columna '{col}' agregada a usuarios")
        except:
            pass

    try:
        c.execute("INSERT INTO usuarios (nombre, email, password, rol) VALUES (?, ?, ?, ?)",
            ('Super Admin', 'admin@lexdoc.com',
             generate_password_hash('admin123'), 'superadmin'))
    except:
        pass

    conn.commit()
    conn.close()


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

# ══════════════════════════════════════════
#  LOGIN / LOGOUT
# ══════════════════════════════════════════
@app.route('/')
def index():
    if 'usuario_id' not in session:
        return redirect(url_for('login'))
    return redirect(url_for('dashboard'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form['email']
        password = request.form['password']

        conn = get_db()
        usuario = conn.execute(
            "SELECT * FROM usuarios WHERE email = ?", (email,)
        ).fetchone()
        conn.close()

        if usuario and check_password_hash(usuario['password'], password):
            session['usuario_id'] = usuario['id']
            session['usuario_nombre'] = usuario['nombre']
            session['rol'] = usuario['rol']
            return redirect(url_for('dashboard'))
        else:
            flash('Email o contraseña incorrectos', 'error')

    return render_template('login.html')

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
    usuarios = conn.execute(
        "SELECT * FROM usuarios WHERE rol != 'superadmin'"
    ).fetchall()
    documentos = conn.execute(
        '''SELECT d.*, u.nombre as abogado
           FROM documentos d
           JOIN usuarios u ON d.abogado_id = u.id'''
    ).fetchall()
    conn.close()
    return render_template('superadmin/dashboard.html',
                         usuarios=usuarios,
                         documentos=documentos,
                         nombre=session['usuario_nombre'])

@app.route('/superadmin/usuarios')
@login_requerido(['superadmin'])
def superadmin_usuarios():
    conn = get_db()
    usuarios = conn.execute(
        "SELECT * FROM usuarios WHERE rol != 'superadmin'"
    ).fetchall()
    conn.close()
    return render_template('superadmin/usuarios.html',
                         usuarios=usuarios,
                         nombre=session['usuario_nombre'])

@app.route('/superadmin/crear_usuario', methods=['POST'])
@login_requerido(['superadmin'])
def crear_usuario():
    nombre = request.form['nombre']
    email = request.form['email']
    password = request.form['password']
    rol = request.form['rol']
    try:
        conn = get_db()
        conn.execute(
            "INSERT INTO usuarios (nombre, email, password, rol) VALUES (?, ?, ?, ?)",
            (nombre, email, generate_password_hash(password), rol)
        )
        conn.commit()
        conn.close()
        flash(f'Usuario {nombre} creado correctamente', 'success')
    except:
        flash('El email ya está registrado', 'error')
    return redirect(url_for('superadmin_usuarios'))

@app.route('/superadmin/eliminar_usuario/<int:id>')
@login_requerido(['superadmin'])
def eliminar_usuario(id):
    conn = get_db()
    conn.execute("DELETE FROM usuarios WHERE id = ?", (id,))
    conn.commit()
    conn.close()
    flash('Usuario eliminado', 'success')
    return redirect(url_for('superadmin_usuarios'))

@app.route('/superadmin/editar_usuario/<int:id>', methods=['GET', 'POST'])
@login_requerido(['superadmin'])
def editar_usuario(id):
    conn = get_db()
    if request.method == 'POST':
        nombre = request.form['nombre']
        email = request.form['email']
        rol = request.form['rol']
        nueva_password = request.form.get('password')
        if nueva_password:
            conn.execute(
                "UPDATE usuarios SET nombre=?, email=?, rol=?, password=? WHERE id=?",
                (nombre, email, rol, generate_password_hash(nueva_password), id)
            )
        else:
            conn.execute(
                "UPDATE usuarios SET nombre=?, email=?, rol=? WHERE id=?",
                (nombre, email, rol, id)
            )
        conn.commit()
        conn.close()
        flash('Usuario actualizado correctamente', 'success')
        return redirect(url_for('superadmin_usuarios'))

    usuario = conn.execute(
        "SELECT * FROM usuarios WHERE id = ?", (id,)
    ).fetchone()
    conn.close()
    return render_template('superadmin/editar_usuario.html',
                         usuario=usuario,
                         nombre=session['usuario_nombre'])

# ══════════════════════════════════════════
#  JEFE DE FIRMA
# ══════════════════════════════════════════
@app.route('/jefe')
@login_requerido(['jefe'])
def jefe_dashboard():
    conn = get_db()
    abogados = conn.execute(
        "SELECT * FROM usuarios WHERE rol = 'abogado'"
    ).fetchall()
    documentos = conn.execute(
        '''SELECT d.*, u.nombre as abogado FROM documentos d
           JOIN usuarios u ON d.abogado_id = u.id
           ORDER BY d.fecha_vencimiento ASC'''
    ).fetchall()
    conn.close()

    hoy = datetime.now().date()
    docs_con_estado = []
    for doc in documentos:
        vencimiento = datetime.strptime(doc['fecha_vencimiento'], '%Y-%m-%d').date()
        dias = (vencimiento - hoy).days
        estado = 'vencido' if dias < 0 else 'urgente' if dias <= 7 else 'proximo' if dias <= 15 else 'ok'
        docs_con_estado.append((doc, estado, dias))

    return render_template('jefe/dashboard.html',
                         abogados=abogados,
                         documentos=docs_con_estado,
                         nombre=session['usuario_nombre'])

@app.route('/jefe/asignar', methods=['GET', 'POST'])
@login_requerido(['jefe'])
def jefe_asignar():
    conn = get_db()
    if request.method == 'POST':
        abogado_id = request.form.get('abogado_id')
        if not abogado_id:
            flash('Debes seleccionar un abogado', 'error')
            abogados = conn.execute(
                "SELECT * FROM usuarios WHERE rol = 'abogado'"
            ).fetchall()
            conn.close()
            return render_template('jefe/asignar.html',
                                 abogados=abogados,
                                 nombre=session['usuario_nombre'])

        titulo = request.form['titulo']
        cliente = request.form['cliente']
        fecha_vencimiento = request.form['fecha_vencimiento']
        notas = request.form.get('notas', '')
        archivo = request.files['archivo']

        nombre_archivo = secure_filename(archivo.filename)
        archivo.save(os.path.join(app.config['UPLOAD_FOLDER'], nombre_archivo))

        conn.execute(
            '''INSERT INTO documentos
               (titulo, cliente, archivo, fecha_vencimiento, notas,
                estado_caso, abogado_id, asignado_por)
               VALUES (?, ?, ?, ?, ?, 'pendiente', ?, ?)''',
            (titulo, cliente, nombre_archivo, fecha_vencimiento,
             notas, abogado_id, session['usuario_id'])
        )
        conn.commit()
        conn.close()
        flash('Caso asignado correctamente', 'success')
        return redirect(url_for('jefe_dashboard'))

    abogados = conn.execute(
        "SELECT * FROM usuarios WHERE rol = 'abogado'"
    ).fetchall()
    conn.close()
    return render_template('jefe/asignar.html',
                         abogados=abogados,
                         nombre=session['usuario_nombre'])

@app.route('/jefe/editar/<int:id>', methods=['GET', 'POST'])
@login_requerido(['jefe'])
def jefe_editar(id):
    conn = get_db()
    doc = conn.execute(
        "SELECT * FROM documentos WHERE id = ?", (id,)
    ).fetchone()

    if not doc:
        flash('Documento no encontrado', 'error')
        return redirect(url_for('jefe_dashboard'))

    if request.method == 'POST':
        titulo = request.form['titulo']
        cliente = request.form['cliente']
        fecha_vencimiento = request.form['fecha_vencimiento']
        notas = request.form.get('notas', '')
        abogado_id = request.form.get('abogado_id')

        conn.execute(
            '''UPDATE documentos SET titulo=?, cliente=?,
               fecha_vencimiento=?, notas=?, abogado_id=?,
               fecha_actualizacion=? WHERE id=?''',
            (titulo, cliente, fecha_vencimiento, notas,
             abogado_id, datetime.now().strftime('%Y-%m-%d %H:%M:%S'), id)
        )
        conn.commit()
        conn.close()
        flash('Caso actualizado correctamente', 'success')
        return redirect(url_for('jefe_dashboard'))

    abogados = conn.execute(
        "SELECT * FROM usuarios WHERE rol = 'abogado'"
    ).fetchall()
    conn.close()
    return render_template('jefe/editar.html',
                         doc=doc,
                         abogados=abogados,
                         nombre=session['usuario_nombre'])

@app.route('/jefe/eliminar/<int:id>')
@login_requerido(['jefe'])
def jefe_eliminar(id):
    conn = get_db()
    conn.execute("DELETE FROM documentos WHERE id = ?", (id,))
    conn.commit()
    conn.close()
    flash('Caso eliminado correctamente', 'success')
    return redirect(url_for('jefe_dashboard'))

# ══════════════════════════════════════════
#  ABOGADO
# ══════════════════════════════════════════
@app.route('/abogado')
@login_requerido(['abogado'])
def abogado_dashboard():
    conn = get_db()
    documentos = conn.execute(
        '''SELECT * FROM documentos WHERE abogado_id = ?
           ORDER BY fecha_vencimiento ASC''',
        (session['usuario_id'],)
    ).fetchall()
    conn.close()

    hoy = datetime.now().date()
    docs_con_estado = []
    for doc in documentos:
        vencimiento = datetime.strptime(doc['fecha_vencimiento'], '%Y-%m-%d').date()
        dias = (vencimiento - hoy).days
        estado = 'vencido' if dias < 0 else 'urgente' if dias <= 7 else 'proximo' if dias <= 15 else 'ok'
        docs_con_estado.append((doc, estado, dias))

    return render_template('abogado/dashboard.html',
                         documentos=docs_con_estado,
                         nombre=session['usuario_nombre'])

@app.route('/abogado/subir', methods=['GET', 'POST'])
@login_requerido(['abogado'])
def abogado_subir():
    if request.method == 'POST':
        titulo = request.form['titulo']
        cliente = request.form['cliente']
        fecha_vencimiento = request.form['fecha_vencimiento']
        notas = request.form.get('notas', '')
        comentario = request.form.get('comentario_abogado', '')
        archivo = request.files['archivo']

        nombre_archivo = secure_filename(archivo.filename)
        archivo.save(os.path.join(app.config['UPLOAD_FOLDER'], nombre_archivo))

        conn = get_db()
        conn.execute(
            '''INSERT INTO documentos
               (titulo, cliente, archivo, fecha_vencimiento, notas,
                comentario_abogado, estado_caso, abogado_id, asignado_por)
               VALUES (?, ?, ?, ?, ?, ?, 'en_proceso', ?, ?)''',
            (titulo, cliente, nombre_archivo, fecha_vencimiento,
             notas, comentario, session['usuario_id'], session['usuario_id'])
        )
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
    doc = conn.execute(
        "SELECT * FROM documentos WHERE id = ? AND abogado_id = ?",
        (id, session['usuario_id'])
    ).fetchone()

    if not doc:
        flash('Documento no encontrado', 'error')
        return redirect(url_for('abogado_dashboard'))

    if request.method == 'POST':
        titulo = request.form['titulo']
        cliente = request.form['cliente']
        fecha_vencimiento = request.form['fecha_vencimiento']
        notas = request.form.get('notas', '')
        comentario = request.form.get('comentario_abogado', '')
        estado_caso = request.form.get('estado_caso', 'pendiente')

        conn.execute(
            '''UPDATE documentos SET titulo=?, cliente=?,
               fecha_vencimiento=?, notas=?, comentario_abogado=?,
               estado_caso=?, fecha_actualizacion=? WHERE id=?''',
            (titulo, cliente, fecha_vencimiento, notas,
             comentario, estado_caso,
             datetime.now().strftime('%Y-%m-%d %H:%M:%S'), id)
        )
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
@app.route('/descargar/<nombre_archivo>')
def descargar(nombre_archivo):
    if 'usuario_id' not in session:
        return redirect(url_for('login'))
    return send_from_directory(
        os.path.abspath(app.config['UPLOAD_FOLDER']),
        nombre_archivo
    )

# ══════════════════════════════════════════
#  ALERTAS POR EMAIL
# ══════════════════════════════════════════
def enviar_alertas():
    conn = get_db()
    hoy = datetime.now().date()
    limite = hoy + timedelta(days=7)

    docs = conn.execute(
        '''SELECT d.id, d.titulo, d.cliente, d.fecha_vencimiento,
                  u.email, u.nombre
           FROM documentos d JOIN usuarios u ON d.abogado_id = u.id
           WHERE d.fecha_vencimiento BETWEEN ? AND ?
           AND d.alerta_enviada = 0''',
        (hoy.strftime('%Y-%m-%d'), limite.strftime('%Y-%m-%d'))
    ).fetchall()

    for doc in docs:
        enviado = enviar_email(doc['email'], doc['nombre'],
                    doc['titulo'], doc['cliente'],
                    doc['fecha_vencimiento'])
        if enviado:
            conn.execute(
                "UPDATE documentos SET alerta_enviada = 1 WHERE id = ?",
                (doc['id'],)
            )
            conn.commit()

    conn.close()

    for doc in docs:
        enviar_email(doc['email'], doc['nombre'],
                    doc['titulo'], doc['cliente'],
                    doc['fecha_vencimiento'])

def enviar_email(destinatario, nombre, titulo, cliente, vencimiento):
    EMAIL = "lexdoc.firma@gmail.com"
    PASSWORD = "pzfhlakjinclkhif"

    mensaje = f"""
    Hola {nombre},

    El documento "{titulo}" del cliente {cliente}
    vence el {vencimiento}.

    Por favor toma las acciones necesarias a tiempo.

    — Sistema LexDoc
    """
    msg = MIMEText(mensaje)
    msg['Subject'] = f'⚠️ Documento por vencer: {titulo}'
    msg['From'] = EMAIL
    msg['To'] = destinatario

    try:
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as smtp:
            smtp.login(EMAIL, PASSWORD)
            smtp.send_message(msg)
        print(f"✅ Email enviado a {destinatario}")
        return True
    except Exception as e:
        print(f"❌ Error: {e}")
        return False

# ══════════════════════════════════════════
#  INICIAR
# ══════════════════════════════════════════
if __name__ == '__main__':
    init_db()
    scheduler = BackgroundScheduler()
    scheduler.add_job(enviar_alertas, 'interval', hours=24)
    scheduler.start()
    app.run(debug=True)