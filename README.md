# LexDoc

Sistema de gestión y asignación de casos jurídicos para firmas de abogados. Control de acceso por roles, gestión documental y alertas automáticas por correo antes del vencimiento de un proceso.

Desarrollado bajo contrato de prestación de servicios para Turizo Lawyers Enterprise S.A. (enero 2024 – mayo 2025).

**Demo pública:** [lexdoc.onrender.com](https://lexdoc.onrender.com)

---

## Qué resuelve

Una firma de abogados maneja decenas de procesos en paralelo, cada uno con documentos asociados y una fecha de vencimiento que no se puede pasar por alto. Sin un sistema, esa información vive en carpetas compartidas y en la memoria de quien asignó el caso.

LexDoc centraliza tres cosas:

- **Quién ve qué.** Cada abogado accede únicamente a los casos que le fueron asignados.
- **Dónde están los documentos.** Carga y descarga desde la aplicación, asociados al caso.
- **Cuándo vence.** El sistema revisa los vencimientos y envía un correo al abogado responsable antes de la fecha límite.

---

## Roles

La aplicación define tres perfiles. El rol se guarda en la tabla `usuarios` y determina a qué rutas puede entrar cada sesión.

| Rol | Permisos |
|---|---|
| **Superadmin** | Crea, edita y elimina usuarios. Gestiona su propio perfil y contraseña. |
| **Jefe** | Asigna casos a los abogados. Edita y elimina cualquier caso. Ve todos los procesos. |
| **Abogado** | Sube documentos, edita los casos asignados y registra comentarios. Solo ve lo propio. |

El aislamiento no es solo de interfaz: las consultas del panel de abogado filtran por `abogado_id`, así que una sesión de abogado no puede recuperar casos de otro ni cambiando la URL. Esa garantía está cubierta por pruebas automatizadas, ver la sección [Pruebas](#pruebas).

---

## Alertas de vencimiento

El módulo que más valor operativo aporta.

1. Una tarea en segundo plano (APScheduler) consulta los documentos cuyo `fecha_vencimiento` cae dentro de los próximos 7 días.
2. Filtra los que aún tienen `alerta_enviada = 0`, para no notificar dos veces el mismo caso.
3. Envía un correo al abogado responsable a través de la API de Resend, con el título del documento, el cliente y la fecha.
4. Marca el registro como notificado.

Existe una ruta `/reset-alertas` que devuelve el flag a cero, usada para reactivar las notificaciones en el entorno de demostración.

---

## Modo invitado

La demo pública expone tres cuentas de prueba, una por rol, accesibles desde `/invitado/<rol>` sin necesidad de credenciales.

Si la base de datos es nueva o quedó incompleta, la ruta crea la cuenta demo en el momento. Esto evita que el botón falle tras un redespliegue o una reinstalación de la base, que es lo que ocurre en el plan gratuito de Render.

---

## Modelo de datos

PostgreSQL. Dos tablas relacionadas por `abogado_id`.

**usuarios**

| Columna | Tipo | Nota |
|---|---|---|
| `id` | SERIAL PK | |
| `nombre` | TEXT | |
| `email` | TEXT UNIQUE | Identificador de login |
| `password` | TEXT | Hash, no texto plano |
| `rol` | TEXT | `superadmin`, `jefe` o `abogado` |

**documentos**

| Columna | Tipo | Nota |
|---|---|---|
| `id` | SERIAL PK | |
| `titulo` | TEXT NOT NULL | |
| `cliente` | TEXT NOT NULL | |
| `archivo` | TEXT NOT NULL | Nombre del archivo almacenado |
| `fecha_vencimiento` | TEXT | Base de las alertas |
| `notas` | TEXT | |
| `comentario_abogado` | TEXT | Seguimiento del responsable |
| `estado_caso` | TEXT | Por defecto `pendiente` |
| `abogado_id` | INTEGER | Abogado asignado |
| `asignado_por` | INTEGER | Quién hizo la asignación |
| `fecha_subida` | TIMESTAMP | |
| `fecha_actualizacion` | TIMESTAMP | |
| `alerta_enviada` | INTEGER | Evita correos duplicados |

Las tablas se crean con `CREATE TABLE IF NOT EXISTS` y las columnas nuevas se agregan con `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`. El arranque es idempotente: la aplicación puede reiniciarse sobre una base existente sin romper nada ni perder datos.

---

## Pruebas

Siete pruebas automatizadas con `pytest` cubren lo que sostiene el sistema:

| Prueba | Qué verifica |
|---|---|
| Sin sesión no se entra a rutas protegidas | `/jefe`, `/abogado` y `/superadmin` redirigen al login |
| Contraseña incorrecta no abre sesión | Autenticación |
| Login correcto guarda el rol | La sesión queda con el rol del usuario |
| Un abogado no entra a rutas de jefe | El decorador `login_requerido` |
| Un abogado sí entra a su propio panel | Contraparte de la anterior |
| **Un abogado no abre el caso de otro abogado** | El filtro `WHERE id = %s AND abogado_id = %s` |
| Un abogado no ve casos ajenos en su panel | El listado también filtra |

La sexta es la central. Si alguien quita ese `AND` de la consulta, la prueba falla y lo señala antes de que llegue a producción.

Requieren una base PostgreSQL accesible. Para levantar una desechable:

```bash
docker run -d --name pg-test \
  -e POSTGRES_PASSWORD=test \
  -e POSTGRES_DB=lexdoc_test \
  -p 5433:5432 \
  postgres:16-alpine
```

Ejecución:

```bash
DATABASE_URL="postgresql://postgres:test@localhost:5433/lexdoc_test" \
SECRET_KEY="clave-de-prueba" \
pytest -v
```

Las pruebas crean sus propios usuarios y casos, y los eliminan al terminar.

---

## Stack

| Componente | Tecnología |
|---|---|
| Lenguaje | Python |
| Framework | Flask |
| Base de datos | PostgreSQL (`psycopg2`) |
| Tareas programadas | APScheduler |
| Correo | Resend |
| Pruebas | pytest |
| Servidor | Gunicorn |
| Despliegue | Render |

---

## Ejecución local

```bash
git clone https://github.com/Carlosp0607/lexdoc.git
cd lexdoc
pip install -r requirements.txt
```

Variables de entorno requeridas:

```
DATABASE_URL=postgresql://usuario:clave@host:5432/basededatos
SECRET_KEY=cadena_aleatoria_para_las_sesiones
RESEND_API_KEY=clave_de_resend
```

```bash
python app.py
```

Las tablas se crean solas en el primer arranque.

---

## Estructura

```
app.py                  Rutas, lógica de negocio, esquema y alertas
wsgi.py                 Punto de entrada para Gunicorn
pytest.ini              Configuración de pytest
requirements.txt
templates/
  login.html
  superadmin/           Gestión de usuarios y perfil
  jefe/                 Asignación y edición de casos
  abogado/              Panel, carga y edición de casos propios
tests/
  test_lexdoc.py        Pruebas de acceso por roles y aislamiento de casos
```

---

## Estado

En funcionamiento. Desplegado en Render con base de datos PostgreSQL gestionada.

La demo corre en el plan gratuito, donde la instancia entra en reposo tras un periodo de inactividad. La primera petición puede tardar cerca de 50 segundos en responder.
