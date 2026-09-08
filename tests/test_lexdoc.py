"""
Pruebas de control de acceso por roles y autenticacion de LexDoc.

Verifican las tres garantias que sostiene el sistema:
  1. Sin sesion no se entra a ninguna ruta protegida.
  2. Un rol no puede usar las rutas de otro rol.
  3. Un abogado solo puede abrir los casos que le fueron asignados.

Ejecutar:  pytest -v
"""

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
    c.execute("DELETE FROM usuarios WHERE email LIKE '%@test.local'")
    conn.commit()
    conn.close()


@pytest.fixture
def cliente():
    """Un navegador simulado, con cookies propias en cada prueba."""
    lexdoc.app.config["TESTING"] = True
    with lexdoc.app.test_client() as c:
        yield c


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