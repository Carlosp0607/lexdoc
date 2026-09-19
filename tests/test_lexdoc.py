"""
Pruebas de control de acceso por roles y autenticacion de LexDoc.

Verifican las tres garantias que sostiene el sistema:
  1. Sin sesion no se entra a ninguna ruta protegida.
  2. Un rol no puede usar las rutas de otro rol.
  3. Un abogado solo puede abrir los casos que le fueron asignados.

Ejecutar:  pytest -v
"""

import io
import os
import pytest
from werkzeug.security import generate_password_hash

import app as lexdoc


# ─────────────────────────────────────────────
#  Preparacion
# ─────────────────────────────────────────────

@pytest.fixture(scope="session", autouse=True)
def esquema():
    """Crea el esquema y los datos de prueba una sola vez."""
    lexdoc.init_db()

    conn = lexdoc.get_db()
    c = conn.cursor()

    # Limpieza por si quedaron datos de una corrida anterior
    c.execute("DELETE FROM documentos WHERE cliente = 'CLIENTE PRUEBA'")
    c.execute("DELETE FROM usuarios WHERE email LIKE '%@test.local'")

    def crear_usuario(nombre, email, rol):
        c.execute(
            "INSERT INTO usuarios (nombre, email, password, rol) "
            "VALUES (%s, %s, %s, %s) RETURNING id",
            (nombre, email, generate_password_hash("clave123"), rol),
        )
        return c.fetchone()["id"]

    ids = {
        "admin": crear_usuario("Admin Prueba", "admin@test.local", "superadmin"),
        "jefe": crear_usuario("Jefe Prueba", "jefe@test.local", "jefe"),
        "abogado_1": crear_usuario("Abogado Uno", "abogado1@test.local", "abogado"),
        "abogado_2": crear_usuario("Abogado Dos", "abogado2@test.local", "abogado"),
    }

    # Un caso asignado al abogado 2. El abogado 1 no debe poder verlo.
    c.execute(
        "INSERT INTO documentos (titulo, cliente, archivo, fecha_vencimiento, abogado_id, "
        "asignado_por, alerta_enviada) VALUES (%s, %s, %s, %s, %s, %s, 0) RETURNING id",
        ("Caso del abogado 2", "CLIENTE PRUEBA", "prueba.pdf", "2026-12-31",
         ids["abogado_2"], ids["jefe"]),
    )
    ids["caso_de_abogado_2"] = c.fetchone()["id"]

    conn.commit()
    conn.close()

    yield ids

    # Limpieza al terminar
    conn = lexdoc.get_db()
    c = conn.cursor()
    c.execute("DELETE FROM documentos WHERE cliente = 'CLIENTE PRUEBA'")
    c.execute("DELETE FROM archivos WHERE nombre NOT IN (SELECT archivo FROM documentos)")
    c.execute("DELETE FROM usuarios WHERE email LIKE '%@test.local'")
    conn.commit()
    conn.close()


@pytest.fixture
def cliente():
    """Un navegador simulado, con cookies propias en cada prueba."""
    lexdoc.app.config["TESTING"] = True
    lexdoc.app.config["WTF_CSRF_ENABLED"] = False  # el token se prueba aparte
    with lexdoc.app.test_client() as c:
        yield c


def limpiar_intentos():
    conn = lexdoc.get_db()
    c = conn.cursor()
    c.execute("DELETE FROM intentos_login WHERE email LIKE '%@test.local'")
    conn.commit()
    conn.close()


def entrar(cliente, email, password="clave123"):
    return cliente.post(
        "/login",
        data={"email": email, "password": password},
        follow_redirects=False,
    )


# ─────────────────────────────────────────────
#  1. Autenticacion
# ─────────────────────────────────────────────

def test_sin_sesion_no_entra_a_rutas_protegidas(cliente):
    """Visitante anonimo: toda ruta protegida lo devuelve al login."""
    for ruta in ("/jefe", "/abogado", "/superadmin"):
        respuesta = cliente.get(ruta)
        assert respuesta.status_code == 302, f"{ruta} no redirigio"
        assert "/login" in respuesta.headers["Location"], f"{ruta} no fue al login"


def test_login_con_password_incorrecta_no_abre_sesion(cliente):
    """Una clave equivocada no debe crear sesion."""
    entrar(cliente, "jefe@test.local", password="clave-que-no-es")

    with cliente.session_transaction() as sesion:
        assert "usuario_id" not in sesion

    # Y sigue sin poder entrar
    assert cliente.get("/jefe").status_code == 302


def test_login_correcto_guarda_el_rol_en_la_sesion(cliente):
    """Credenciales validas: la sesion queda con el rol del usuario."""
    respuesta = entrar(cliente, "jefe@test.local")

    assert respuesta.status_code == 302
    with cliente.session_transaction() as sesion:
        assert sesion["rol"] == "jefe"
        assert "usuario_id" in sesion


# ─────────────────────────────────────────────
#  2. Separacion de roles
# ─────────────────────────────────────────────

def test_abogado_no_entra_a_rutas_de_jefe(cliente):
    """El decorador login_requerido debe frenar a un rol ajeno."""
    entrar(cliente, "abogado1@test.local")

    respuesta = cliente.get("/jefe")

    assert respuesta.status_code == 302
    assert "/jefe" not in respuesta.headers["Location"]


def test_abogado_si_entra_a_su_propio_panel(cliente):
    """Contraparte del anterior: su panel si debe abrir."""
    entrar(cliente, "abogado1@test.local")

    assert cliente.get("/abogado").status_code == 200


# ─────────────────────────────────────────────
#  3. Aislamiento de casos entre abogados
# ─────────────────────────────────────────────

def test_abogado_no_abre_el_caso_de_otro_abogado(cliente, esquema):
    """
    La garantia central del sistema.

    La consulta de /abogado/editar/<id> filtra por id Y por abogado_id.
    Si alguien quita ese AND, esta prueba falla.
    """
    entrar(cliente, "abogado1@test.local")

    ajeno = esquema["caso_de_abogado_2"]
    respuesta = cliente.get(f"/abogado/editar/{ajeno}", follow_redirects=False)

    assert respuesta.status_code == 302, "dejo abrir un caso ajeno"


def test_abogado_no_ve_casos_ajenos_en_su_panel(cliente):
    """El listado del panel tampoco debe mostrar casos de otro abogado."""
    entrar(cliente, "abogado1@test.local")

    cuerpo = cliente.get("/abogado").get_data(as_text=True)

    assert "Caso del abogado 2" not in cuerpo

# ─────────────────────────────────────────────
#  4. Integridad de datos y borrado seguro
# ─────────────────────────────────────────────

def test_eliminar_no_acepta_get(cliente):
    """Borrar es una accion que modifica datos: solo por POST."""
    entrar(cliente, "jefe@test.local")
    assert cliente.get("/jefe/eliminar/999999").status_code == 405


def test_no_se_borra_abogado_con_casos(cliente, esquema):
    """La llave foranea impide dejar casos huerfanos."""
    entrar(cliente, "admin@test.local")
    cliente.post(f"/superadmin/eliminar_usuario/{esquema['abogado_2']}")

    conn = lexdoc.get_db()
    c = conn.cursor()
    c.execute("SELECT id FROM usuarios WHERE id = %s", (esquema["abogado_2"],))
    sigue = c.fetchone()
    conn.close()
    assert sigue is not None


@pytest.mark.parametrize("dias, esperado", [
    (-1, "vencido"), (0, "urgente"), (7, "urgente"),
    (8, "proximo"), (15, "proximo"), (16, "ok"),
])
def test_calcular_estado_mismos_umbrales(dias, esperado):
    from datetime import date, timedelta
    fecha = date.today() + timedelta(days=dias)
    estado, restantes = lexdoc.calcular_estado(fecha)
    assert estado == esperado
    assert restantes == dias



# ─────────────────────────────────────────────
#  5. Seguridad adicional
# ─────────────────────────────────────────────

def test_post_sin_token_csrf_es_rechazado():
    lexdoc.app.config["WTF_CSRF_ENABLED"] = True
    try:
        with lexdoc.app.test_client() as c:
            c.post("/login", data={"email": "jefe@test.local", "password": "clave123"})
            with c.session_transaction() as sesion:
                assert "usuario_id" not in sesion
    finally:
        lexdoc.app.config["WTF_CSRF_ENABLED"] = False


def test_abogado_no_descarga_documento_ajeno(cliente):
    entrar(cliente, "abogado1@test.local")
    assert cliente.get("/descargar/prueba.pdf").status_code == 404


def test_abogado_si_descarga_su_documento(cliente):
    entrar(cliente, "abogado2@test.local")
    assert cliente.get("/descargar/prueba.pdf").status_code == 200


def test_login_se_bloquea_tras_cinco_intentos(cliente):
    try:
        for _ in range(5):
            entrar(cliente, "abogado1@test.local", password="mala")
        entrar(cliente, "abogado1@test.local")  # clave correcta, pero bloqueado
        with cliente.session_transaction() as sesion:
            assert "usuario_id" not in sesion
    finally:
        limpiar_intentos()


def test_no_se_crea_usuario_con_rol_superadmin(cliente):
    entrar(cliente, "admin@test.local")
    cliente.post("/superadmin/crear_usuario", data={
        "nombre": "Intruso", "email": "intruso@test.local",
        "password": "clave12345", "rol": "superadmin"})
    conn = lexdoc.get_db()
    c = conn.cursor()
    c.execute("SELECT 1 FROM usuarios WHERE email = 'intruso@test.local'")
    creado = c.fetchone()
    conn.close()
    assert creado is None


def test_superadmin_no_se_puede_eliminar(cliente, esquema):
    entrar(cliente, "admin@test.local")
    cliente.post(f"/superadmin/eliminar_usuario/{esquema['admin']}")
    conn = lexdoc.get_db()
    c = conn.cursor()
    c.execute("SELECT 1 FROM usuarios WHERE id = %s", (esquema["admin"],))
    sigue = c.fetchone()
    conn.close()
    assert sigue is not None


def test_fecha_invalida_no_se_guarda(cliente, esquema):
    entrar(cliente, "abogado2@test.local")
    cliente.post(f"/abogado/editar/{esquema['caso_de_abogado_2']}", data={
        "titulo": "Caso del abogado 2", "cliente": "CLIENTE PRUEBA",
        "fecha_vencimiento": "manana", "estado_caso": "pendiente"})
    conn = lexdoc.get_db()
    c = conn.cursor()
    c.execute("SELECT fecha_vencimiento FROM documentos WHERE id = %s",
              (esquema["caso_de_abogado_2"],))
    fecha = str(c.fetchone()["fecha_vencimiento"])
    conn.close()
    assert fecha == "2026-12-31"



# ─────────────────────────────────────────────
#  6. Estados, archivos reales, modo demo y CSP
# ─────────────────────────────────────────────

PDF_REAL = b"%PDF-1.4 contenido de prueba"


def test_estado_listo_es_aceptado(cliente, esquema):
    entrar(cliente, "abogado2@test.local")
    cliente.post(f"/abogado/editar/{esquema['caso_de_abogado_2']}", data={
        "titulo": "Caso del abogado 2", "cliente": "CLIENTE PRUEBA",
        "fecha_vencimiento": "2026-12-31", "estado_caso": "listo"})
    conn = lexdoc.get_db()
    c = conn.cursor()
    c.execute("SELECT estado_caso FROM documentos WHERE id = %s",
              (esquema["caso_de_abogado_2"],))
    estado = c.fetchone()["estado_caso"]
    conn.close()
    assert estado == "listo"


def test_intentos_fallidos_quedan_en_la_base(cliente):
    try:
        entrar(cliente, "abogado1@test.local", password="mala")
        conn = lexdoc.get_db()
        c = conn.cursor()
        c.execute("SELECT COUNT(*) AS n FROM intentos_login WHERE email = 'abogado1@test.local'")
        n = c.fetchone()["n"]
        conn.close()
        assert n == 1
    finally:
        limpiar_intentos()


def test_fuera_de_demo_se_guarda_y_descarga_el_archivo_real(cliente, monkeypatch):
    monkeypatch.setattr(lexdoc, "MODO_DEMO", False)
    entrar(cliente, "abogado2@test.local")
    cliente.post("/abogado/subir", data={
        "titulo": "Caso con archivo", "cliente": "CLIENTE PRUEBA",
        "fecha_vencimiento": "2026-11-30",
        "archivo": (io.BytesIO(PDF_REAL), "poder.pdf")},
        content_type="multipart/form-data")
    conn = lexdoc.get_db()
    c = conn.cursor()
    c.execute("SELECT archivo FROM documentos WHERE titulo = 'Caso con archivo'")
    nombre = c.fetchone()["archivo"]
    conn.close()

    respuesta = cliente.get(f"/descargar/{nombre}")
    assert respuesta.status_code == 200
    assert respuesta.data == PDF_REAL
    assert "attachment" in respuesta.headers["Content-Disposition"]


def test_archivo_con_extension_falsa_es_rechazado(cliente, monkeypatch):
    monkeypatch.setattr(lexdoc, "MODO_DEMO", False)
    entrar(cliente, "abogado2@test.local")
    cliente.post("/abogado/subir", data={
        "titulo": "Caso falso", "cliente": "CLIENTE PRUEBA",
        "fecha_vencimiento": "2026-11-30",
        "archivo": (io.BytesIO(b"MZ ejecutable disfrazado"), "factura.pdf")},
        content_type="multipart/form-data")
    conn = lexdoc.get_db()
    c = conn.cursor()
    c.execute("SELECT 1 FROM documentos WHERE titulo = 'Caso falso'")
    creado = c.fetchone()
    conn.close()
    assert creado is None


def test_sin_modo_demo_no_hay_acceso_de_invitado(cliente, monkeypatch):
    monkeypatch.setattr(lexdoc, "MODO_DEMO", False)
    assert cliente.get("/invitado/superadmin").status_code == 404


def test_paginas_llevan_politica_csp(cliente):
    politica = cliente.get("/login").headers.get("Content-Security-Policy", "")
    assert "script-src 'self'" in politica
    assert "unsafe-inline" not in politica.split("script-src")[1].split(";")[0]


# ─────────────────────────────────────────────
#  7. Sesion, papelera, auditoria, recuperacion y alertas
# ─────────────────────────────────────────────

def consulta(sql, params=()):
    conn = lexdoc.get_db()
    c = conn.cursor()
    c.execute(sql, params)
    fila = c.fetchone()
    conn.close()
    return fila


def ejecutar(sql, params=()):
    conn = lexdoc.get_db()
    c = conn.cursor()
    c.execute(sql, params)
    fila = c.fetchone() if c.description else None
    conn.commit()
    conn.close()
    return fila


def test_sesion_vence_por_inactividad(cliente):
    import time
    from datetime import timedelta
    original = lexdoc.app.config["PERMANENT_SESSION_LIFETIME"]
    try:
        entrar(cliente, "jefe@test.local")
        assert cliente.get("/jefe").status_code == 200
        lexdoc.app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(seconds=1)
        time.sleep(2.1)
        assert cliente.get("/jefe").status_code == 302
    finally:
        lexdoc.app.config["PERMANENT_SESSION_LIFETIME"] = original


def test_papelera_eliminar_y_restaurar(cliente, esquema):
    caso = esquema["caso_de_abogado_2"]
    entrar(cliente, "jefe@test.local")
    cliente.post(f"/jefe/eliminar/{caso}")
    assert consulta("SELECT eliminado_en FROM documentos WHERE id = %s", (caso,))["eliminado_en"]
    assert "Caso del abogado 2" in cliente.get("/jefe/papelera").get_data(as_text=True)

    cliente.post(f"/jefe/restaurar/{caso}")
    assert consulta("SELECT eliminado_en FROM documentos WHERE id = %s", (caso,))["eliminado_en"] is None


def test_eliminar_definitivo_solo_desde_papelera(cliente, esquema):
    caso = ejecutar(
        "INSERT INTO documentos (titulo, cliente, archivo, fecha_vencimiento, abogado_id, asignado_por) "
        "VALUES ('Temporal', 'CLIENTE PRUEBA', 'temporal.pdf', '2026-12-01', %s, %s) RETURNING id",
        (esquema["abogado_1"], esquema["jefe"]))["id"]
    entrar(cliente, "jefe@test.local")

    cliente.post(f"/jefe/eliminar_definitivo/{caso}")  # activo: no se borra
    assert consulta("SELECT 1 FROM documentos WHERE id = %s", (caso,)) is not None

    cliente.post(f"/jefe/eliminar/{caso}")
    cliente.post(f"/jefe/eliminar_definitivo/{caso}")
    assert consulta("SELECT 1 FROM documentos WHERE id = %s", (caso,)) is None


def test_acciones_quedan_en_auditoria(cliente):
    entrar(cliente, "admin@test.local")
    cliente.post("/superadmin/crear_usuario", data={
        "nombre": "Auditado", "email": "auditado@test.local",
        "password": "clave12345", "rol": "abogado"})
    assert consulta("SELECT 1 FROM auditoria WHERE accion = 'crear_usuario' "
                    "AND detalle LIKE 'auditado@test.local%%'") is not None
    assert cliente.get("/superadmin/auditoria").status_code == 200
    ejecutar("DELETE FROM usuarios WHERE email = 'auditado@test.local'")


def test_recuperar_contrasena_de_un_solo_uso(cliente, monkeypatch):
    import re
    enviados = []
    monkeypatch.setattr(lexdoc, "enviar_correo",
                        lambda destino, asunto, texto: enviados.append(texto) or True)
    try:
        cliente.post("/recuperar", data={"email": "abogado1@test.local"})
        assert len(enviados) == 1
        ruta = re.search(r"(/restablecer/\S+)", enviados[0]).group(1)

        cliente.post(ruta, data={"password_nueva": "nuevaclave99",
                                 "password_confirmar": "nuevaclave99"})
        entrar(cliente, "abogado1@test.local", password="nuevaclave99")
        with cliente.session_transaction() as sesion:
            assert "usuario_id" in sesion

        # El mismo enlace ya no sirve
        assert cliente.get(ruta).status_code == 302
    finally:
        ejecutar("UPDATE usuarios SET password = %s WHERE email = 'abogado1@test.local'",
                 (lexdoc.generate_password_hash("clave123"),))


def test_recuperar_no_revela_si_el_correo_existe(cliente, monkeypatch):
    monkeypatch.setattr(lexdoc, "enviar_correo", lambda *a: True)
    existe = cliente.post("/recuperar", data={"email": "abogado1@test.local"},
                          follow_redirects=True).get_data(as_text=True)
    no_existe = cliente.post("/recuperar", data={"email": "nadie@test.local"},
                             follow_redirects=True).get_data(as_text=True)
    mensaje = "Si el correo esta registrado"
    assert mensaje in existe and mensaje in no_existe


def test_alertas_proximas_y_vencidas(esquema, monkeypatch):
    from datetime import date, timedelta
    asuntos = []
    monkeypatch.setattr(lexdoc, "enviar_correo",
                        lambda destino, asunto, texto: asuntos.append(asunto) or True)
    hoy = date.today()
    for titulo, dias, estado in [("Alerta proxima", 3, "pendiente"),
                                 ("Alerta vencida", -2, "en_proceso"),
                                 ("Vencido pero listo", -2, "listo")]:
        ejecutar("INSERT INTO documentos (titulo, cliente, archivo, fecha_vencimiento, "
                 "estado_caso, abogado_id, asignado_por) "
                 "VALUES (%s, 'CLIENTE PRUEBA', 'x.pdf', %s, %s, %s, %s)",
                 (titulo, hoy + timedelta(days=dias), estado,
                  esquema["abogado_1"], esquema["jefe"]))

    lexdoc.enviar_alertas()
    assert "Documento por vencer: Alerta proxima" in asuntos
    assert "VENCIDO: Alerta vencida" in asuntos
    assert not any("Vencido pero listo" in a for a in asuntos)

    asuntos.clear()
    lexdoc.enviar_alertas()  # segunda corrida: no repite
    assert not any("Alerta" in a for a in asuntos)


def test_cambiar_fecha_reactiva_la_alerta(cliente, esquema):
    caso = esquema["caso_de_abogado_2"]
    ejecutar("UPDATE documentos SET alerta_enviada = 1 WHERE id = %s", (caso,))
    entrar(cliente, "abogado2@test.local")
    cliente.post(f"/abogado/editar/{caso}", data={
        "titulo": "Caso del abogado 2", "cliente": "CLIENTE PRUEBA",
        "fecha_vencimiento": "2027-01-15", "estado_caso": "pendiente"})
    assert consulta("SELECT alerta_enviada FROM documentos WHERE id = %s",
                    (caso,))["alerta_enviada"] == 0
    ejecutar("UPDATE documentos SET fecha_vencimiento = '2026-12-31' WHERE id = %s", (caso,))
