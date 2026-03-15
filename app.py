from flask import Flask, render_template, request, redirect, url_for, session, flash, send_from_directory
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from apscheduler.schedulers.background import BackgroundScheduler
import psycopg2
import psycopg2.extras
import os
import resend
from datetime import datetime, timedelta

app = Flask(__name__)
app.secret_key = 'lexdoc-clave-secreta-2024'
UPLOAD_FOLDER = 'uploads'
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

# ══════════════════════════════════════════
#  BASE DE DATOS
# ══════════════════════════════════════════
DATABASE_URL = os.environ.get('DATABASE_URL', 'postgresql://lexdoc_db_user:DSKF0kr3rKIIfHP6Q2hyQZENYM2KuDou@dpg-d6qpev450q8c73bmu5d0-a.oregon-postgres.render.com/lexdoc_db')

def get_db():
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    return conn

def init_db():
    conn = get_db()
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
        fecha_vencimiento TEXT NOT NULL,
        notas TEXT,
        comentario_abogado TEXT,
        estado_caso TEXT DEFAULT 'pendiente',
        abogado_id INTEGER,
        asignado_por INTEGER,
        fecha_subida TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        fecha_actualizacion TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        alerta_enviada INTEGER DEFAULT 0
    )''')

    migraciones = [
        "ALTER TABLE documentos ADD COLUMN IF NOT EXISTS comentario_abogado TEXT",
        "ALTER TABLE documentos ADD COLUMN IF NOT EXISTS estado_caso TEXT DEFAULT 'pendiente'",
        "ALTER TABLE documentos ADD COLUMN IF NOT EXISTS fecha_actualizacion TIMESTAMP DEFAULT CURRENT_TIMESTAMP",
        "ALTER TABLE documentos ADD COLUMN IF NOT EXISTS alerta_enviada INTEGER DEFAULT 0",
    ]
    for sql in migraciones:
        try:
            c.execute(sql)
        except:
            conn.rollback()

    try:
        c.execute("INSERT INTO usuarios (nombre, email, password, rol) VALUES (%s, %s, %s, %s)",
            ('Super Admin', 'admin@lexdoc.com',
             generate_password_hash('admin123'), 'superadmin'))
    except:
        conn.rollback()

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
        c = conn.cursor()
        c.execute("SELECT * FROM usuarios WHERE email = %s", (email,))
        usuario = c.fetchone()
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
    c = conn.cursor()
    c.execute("SELECT * FROM usuarios WHERE rol != 'superadmin'")
    usuarios = c.fetchall()
    c.execute('''SELECT d.*, u.nombre as abogado
               FROM documentos d
               JOIN usuarios u ON d.abogado_id = u.id''')
    documentos = c.fetchall()
    conn.close()

    hoy = datetime.now().date()
    docs_con_estado = []
    for doc in documentos:
        vencimiento = datetime.strptime(str(doc['fecha_vencimiento']), '%Y-%m-%d').date()
        dias = (vencimiento - hoy).days
        estado = 'vencido' if dias < 0 else 'urgente' if dias <= 1 else 'proximo' if dias <= 7 else 'ok'
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
    nombre = request.form['nombre']
    email = request.form['email']
    password = request.form['password']
    rol = request.form['rol']
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute(
            "INSERT INTO usuarios (nombre, email, password, rol) VALUES (%s, %s, %s, %s)",
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
    c = conn.cursor()
    c.execute("DELETE FROM usuarios WHERE id = %s", (id,))
    conn.commit()
    conn.close()
    flash('Usuario eliminado', 'success')
    return redirect(url_for('superadmin_usuarios'))

@app.route('/superadmin/editar_usuario/<int:id>', methods=['GET', 'POST'])
@login_requerido(['superadmin'])
def editar_usuario(id):
    conn = get_db()
    c = conn.cursor()
    if request.method == 'POST':
        nombre = request.form['nombre']
        email = request.form['email']
        rol = request.form['rol']
        nueva_password = request.form.get('password')
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
        conn.commit()
        conn.close()
        flash('Usuario actualizado correctamente', 'success')
        return redirect(url_for('superadmin_usuarios'))

    c.execute("SELECT * FROM usuarios WHERE id = %s", (id,))
    usuario = c.fetchone()
    conn.close()
    return render_template('superadmin/editar_usuario.html',
                         usuario=usuario,
                         nombre=session['usuario_nombre'])

@app.route('/superadmin/perfil', methods=['GET', 'POST'])
@login_requerido(['superadmin'])
def superadmin_perfil():
    conn = get_db()
    c = conn.cursor()
    if request.method == 'POST':
        nombre = request.form.get('nombre')
        email = request.form.get('email')
        try:
            c.execute(
                "UPDATE usuarios SET nombre=%s, email=%s WHERE id=%s",
                (nombre, email, session['usuario_id'])
            )
            conn.commit()
            session['usuario_nombre'] = nombre
            flash('Perfil actualizado correctamente', 'success')
        except:
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
        password_actual = request.form.get('password_actual')
        password_nueva = request.form.get('password_nueva')
        password_confirmar = request.form.get('password_confirmar')

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

        if len(password_nueva) < 6:
            flash('La contraseña debe tener mínimo 6 caracteres', 'error')
            conn.close()
            return redirect(url_for('cambiar_password'))

        c.execute(
            "UPDATE usuarios SET password = %s WHERE id = %s",
            (generate_password_hash(password_nueva), session['usuario_id'])
        )
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
               ORDER BY d.fecha_vencimiento ASC''')
    documentos = c.fetchall()
    conn.close()

    hoy = datetime.now().date()
    docs_con_estado = []
    for doc in documentos:
        vencimiento = datetime.strptime(str(doc['fecha_vencimiento']), '%Y-%m-%d').date()
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
    c = conn.cursor()
    if request.method == 'POST':
        abogado_id = request.form.get('abogado_id')
        if not abogado_id:
            flash('Debes seleccionar un abogado', 'error')
            c.execute("SELECT * FROM usuarios WHERE rol = 'abogado'")
            abogados = c.fetchall()
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
        os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
        archivo.save(os.path.join(app.config['UPLOAD_FOLDER'], nombre_archivo))

        c.execute(
            '''INSERT INTO documentos
               (titulo, cliente, archivo, fecha_vencimiento, notas,
                estado_caso, abogado_id, asignado_por)
               VALUES (%s, %s, %s, %s, %s, 'pendiente', %s, %s)''',
            (titulo, cliente, nombre_archivo, fecha_vencimiento,
             notas, abogado_id, session['usuario_id'])
        )
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
    c.execute("SELECT * FROM documentos WHERE id = %s", (id,))
    doc = c.fetchone()

    if not doc:
        flash('Documento no encontrado', 'error')
        return redirect(url_for('jefe_dashboard'))

    if request.method == 'POST':
        titulo = request.form['titulo']
        cliente = request.form['cliente']
        fecha_vencimiento = request.form['fecha_vencimiento']
        notas = request.form.get('notas', '')
        abogado_id = request.form.get('abogado_id')

        c.execute(
            '''UPDATE documentos SET titulo=%s, cliente=%s,
               fecha_vencimiento=%s, notas=%s, abogado_id=%s,
               fecha_actualizacion=%s WHERE id=%s''',
            (titulo, cliente, fecha_vencimiento, notas,
             abogado_id, datetime.now(), id)
        )
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

@app.route('/jefe/eliminar/<int:id>')
@login_requerido(['jefe'])
def jefe_eliminar(id):
    conn = get_db()
    c = conn.cursor()
    c.execute("DELETE FROM documentos WHERE id = %s", (id,))
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
    c = conn.cursor()
    c.execute(
        '''SELECT * FROM documentos WHERE abogado_id = %s
           ORDER BY fecha_vencimiento ASC''',
        (session['usuario_id'],)
    )
    documentos = c.fetchall()
    conn.close()

    hoy = datetime.now().date()
    docs_con_estado = []
    for doc in documentos:
        vencimiento = datetime.strptime(str(doc['fecha_vencimiento']), '%Y-%m-%d').date()
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
        os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
        archivo.save(os.path.join(app.config['UPLOAD_FOLDER'], nombre_archivo))

        conn = get_db()
        c = conn.cursor()
        c.execute(
            '''INSERT INTO documentos
               (titulo, cliente, archivo, fecha_vencimiento, notas,
                comentario_abogado, estado_caso, abogado_id, asignado_por)
               VALUES (%s, %s, %s, %s, %s, %s, 'en_proceso', %s, %s)''',
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
    c = conn.cursor()
    c.execute(
        "SELECT * FROM documentos WHERE id = %s AND abogado_id = %s",
        (id, session['usuario_id'])
    )
    doc = c.fetchone()

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

        c.execute(
            '''UPDATE documentos SET titulo=%s, cliente=%s,
               fecha_vencimiento=%s, notas=%s, comentario_abogado=%s,
               estado_caso=%s, fecha_actualizacion=%s WHERE id=%s''',
            (titulo, cliente, fecha_vencimiento, notas,
             comentario, estado_caso, datetime.now(), id)
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

@app.route('/reset-alertas')
def reset_alertas():
    conn = get_db()
    c = conn.cursor()
    c.execute('UPDATE documentos SET alerta_enviada = 0')
    conn.commit()
    conn.close()
    return 'Reseteado OK'

# ══════════════════════════════════════════
#  ALERTAS POR EMAIL
# ══════════════════════════════════════════
def enviar_alertas():
    conn = get_db()
    c = conn.cursor()
    hoy = datetime.now().date()
    limite = hoy + timedelta(days=7)

    c.execute(
        '''SELECT d.id, d.titulo, d.cliente, d.fecha_vencimiento,
                  u.email, u.nombre
           FROM documentos d JOIN usuarios u ON d.abogado_id = u.id
           WHERE d.fecha_vencimiento BETWEEN %s AND %s
           AND d.alerta_enviada = 0''',
        (hoy.strftime('%Y-%m-%d'), limite.strftime('%Y-%m-%d'))
    )
    docs = c.fetchall()

    for doc in docs:
        enviado = enviar_email(doc['email'], doc['nombre'],
                    doc['titulo'], doc['cliente'],
                    doc['fecha_vencimiento'])
        if enviado:
            c.execute(
                "UPDATE documentos SET alerta_enviada = 1 WHERE id = %s",
                (doc['id'],)
            )
            conn.commit()

    conn.close()

def enviar_email(destinatario, nombre, titulo, cliente, vencimiento):
    resend.api_key = "re_XMT9tPmw_KdjDKNCYo6e1bM1KgLrgG3uc"
    try:
        resend.Emails.send({
            "from": "onboarding@resend.dev",
            "to": [destinatario],
            "subject": f"⚠️ Documento por vencer: {titulo}",
            "text": f"Hola {nombre},\n\nEl documento '{titulo}' del cliente {cliente} vence el {vencimiento}.\n\nPor favor toma las acciones necesarias.\n\n— Sistema LexDoc"
        })
        print(f"✅ Email enviado a {destinatario}")
        return True
    except Exception as e:
        print(f"❌ Error: {e}")
        return False

# ══════════════════════════════════════════
#  INICIAR
# ══════════════════════════════════════════
init_db()


if __name__ == '__main__':
    app.run(debug=True)