# Plataforma de Logística Internacional — TP Persistencia Políglota

Aplicación de consola en Python que usa cinco motores de base de datos:
PostgreSQL, MongoDB, Neo4j, Cassandra y Redis.

## Requisitos

- **Docker Desktop** (levanta los cinco motores). En Windows usa WSL 2.
- **Python 3.10 o superior** (recomendado 3.11 o 3.12).
- Unos 3 GB de RAM libres para los contenedores (Cassandra y Neo4j son los que más usan).

## Puesta en marcha (Windows, una sola vez)

1. Instalar Docker Desktop desde https://www.docker.com/products/docker-desktop/ ,
   reiniciar y abrirlo. Esperar a que diga *Engine running*.
2. Abrir esta carpeta en PyCharm y crear un entorno virtual
   (*Settings → Project → Python Interpreter → Add Interpreter → Virtualenv*),
   o desde una terminal en la carpeta del proyecto:
   ```
   py -m venv .venv
   .venv\Scripts\activate
   pip install -r requirements.txt
   ```
3. (Opcional) copiar `.env.example` como `.env` para cambiar contraseñas o puertos.
   Si no existe `.env`, se usan los mismos valores por defecto que `docker-compose.yml`.

## Uso diario

```
docker compose up -d                      # levanta los 5 motores (la primera vez descarga las imágenes)
docker compose ps                         # esperar a que los 5 digan "healthy" (Cassandra tarda 1-2 min)
python -m herramientas.inicializar_bases  # crea lo que falte: roles, admin, ubicaciones y tarifas (idempotente)
python -m herramientas.datos_demo         # (opcional, una vez) clientes, operadores, contenedores y envíos de ejemplo
python -m app                             # abre la aplicación (con el sincronizador de Neo4j en segundo plano)
```

Usuarios:

| Email | Contraseña | Rol |
| --- | --- | --- |
| `admin@logistica.local` | `Admin1234` (variable `ADMIN_PASSWORD`) | ADMINISTRADOR (5) |
| `operador@logistica.local` | `Demo12345` | OPERADOR (3), lo crea `datos_demo` |
| `cliente@vinosdelsur.com` | `Demo12345` | CLIENTE (1), solo ve los envíos de Vinos del Sur |

En el menú principal: números para los módulos, `E` estado de las bases, `M` mi sesión, `0` cerrar sesión.

Sincronización con Neo4j (PostgreSQL outbox → Redis `sync:stream` → Neo4j):

```
python -m herramientas.sincronizar --estado       # cuántos eventos hay en cada paso
python -m herramientas.sincronizar --una-vez      # un ciclo a mano
python -m herramientas.sincronizar --reconstruir  # borra el grafo y lo rearma desde PostgreSQL
```

Sensores IoT (lecturas en Cassandra, último contacto y avisos en Redis, alertas en MongoDB):

```
python -m herramientas.simulador_sensores                     # en vivo, cada 5 s (dejarlo en otra terminal)
python -m herramientas.simulador_sensores --anomalias 0.3     # más lecturas fuera de rango
python -m herramientas.simulador_sensores --lote 24 --cada 10 # 24 h de historial de una vez
```

Trazabilidad (fase 4): la línea de tiempo de cada envío se arma sola en MongoDB a partir de los mismos
eventos del outbox (grupo `trazabilidad` del stream). Después de actualizar a la fase 4, correr una vez
`python -m herramientas.inicializar_bases` para crear la restricción `Incidente` en Neo4j.

Mensajería (fase 5): menú 9. Conversaciones privadas y grupos de trabajo en MongoDB; contadores de no leídos,
límite de 20 mensajes por minuto y presencia ("en línea") en Redis.

Comercial (fase 6): menú 10. Facturas de los envíos entregados (con IVA), pagos parciales, anulación, cuenta corriente
con saldo mantenido por trigger, deuda por antigüedad y control de crédito al crear envíos (PostgreSQL); la facturación
masiva usa un lock en Redis (`lock:proceso:facturacion`).

Procesos periódicos y reportes (fase 7): menú 11. Dejar corriendo en otra terminal

```
python -m herramientas.planificador              # cada proceso cuando le toca (Ctrl+C para terminar)
python -m herramientas.planificador --listar     # última ejecución de cada uno
python -m herramientas.planificador --proceso cierre   # correr uno ahora
```

Procesos: reportes (cola en MongoDB), sensores sin contacto, sesiones vencidas, predicción de riesgo, facturación automática,
facturas vencidas y cierre mensual. Cada uno con lock y estado en Redis. Los reportes exportados quedan en `reportes/`.

Datos de prueba y verificación (fase 8):

```
python -m herramientas.datos_prueba     # ~6 meses de historia en los 5 motores (un par de minutos; una sola vez)
python -m herramientas.verificar        # compara los motores entre sí: OK / ADVERTENCIA / ERROR
```

`datos_prueba` simula la operación en el tiempo con un reloj simulado y los servicios reales: unos 90 envíos
(hoy queda al menos uno en cada estado), ~9.000 lecturas IoT, incidentes, facturas y pagos, cierres mensuales
y mensajes. Agrega 10 clientes, 6 operadores, 30 contenedores y 6 usuarios (contraseña `Demo12345`):
`trafico@logistica.local` (OPERADOR), `supervision@logistica.local` (SUPERVISOR), `analisis@logistica.local`
(ANALISTA) y tres de clientes (`rbenitez@frigorificopampeano.com`, `hpaz@electroimport.com`, `lferro@biofarma.com`).
El verificador también está en el menú 6 (opción 4).

Fase 9 (ABM que pide la promoción): menú 5 (alta, modificación y baja de roles; sesiones activas de todos los
usuarios y cierre de sesiones ajenas), menú 1 (modificar prioridad o entrega estimada; agregar y quitar contenedores
antes de que el envío salga) y menú 7 (reglas de alerta configurables, tabla `regla_alerta`). Después de actualizar,
correr una vez `python -m herramientas.inicializar_bases`: aplica `scripts/postgres/02_fase9.sql` (es idempotente).

Fase 9, segunda parte: menú 8 (recorrido de un contenedor con Neo4j + mapa, red de relaciones de un cliente, mapa
GPS de un envío: se guarda en `mapas/` y se abre en el navegador), reportes para clientes que se COBRAN en su próxima
factura (ítem con `id_reporte`; los clientes los piden desde el menú 11), reporte predictivo RIESGO_DEMORA y rol
SOCIO para usuarios de operadores y proveedores (mensajería, su cuenta corriente y los envíos que mueven).
`python -m herramientas.datos_prueba` agrega lo de la fase 9 aunque la carga principal ya esté hecha.

Para apagar: `docker compose down` (los datos se conservan).
Para empezar de cero: `docker compose down -v` (borra los datos) y volver a inicializar.

## Pruebas

```
pytest              # unitarias + integración (las de integración se saltean si los motores no están levantados)
pytest -m "not integracion"   # solo unitarias, no necesitan Docker
```

## Estructura

```
app/
  __main__.py          punto de entrada y armado de dependencias
  config.py            configuración desde variables de entorno (.env)
  logs.py              logs en logs/app.log y logs/errores.log
  excepciones.py       errores de la aplicación
  dominio/             entidades del negocio (sin dependencias de bases)
  infra/               conexiones a los 5 motores
  repositorios/        acceso a datos, un subpaquete por motor (patrón Repository)
  servicios/           reglas de negocio y coordinación entre motores
  presentacion/        menús de consola (rich)
herramientas/          inicialización, datos de demo y de prueba, simulador, sincronizador, planificador, verificador
scripts/               scripts de creación de cada motor (entregable 4)
tests/                 pruebas unitarias e integración
```

## Herramientas útiles para la defensa

- Neo4j Browser: http://localhost:7474 (usuario `neo4j`, clave `tp_clave_neo4j`)
- `docker compose exec postgres psql -U tp -d logistica`
- `docker compose exec mongo mongosh logistica_docs`
- `docker compose exec cassandra cqlsh`
- `docker compose exec redis redis-cli`

## Problemas frecuentes

| Síntoma | Causa y solución |
| --- | --- |
| `docker: command not found` o error de conexión con Docker | Docker Desktop no está abierto |
| Cassandra figura *unhealthy* o *starting* | Tarda 1-2 minutos; esperar y repetir `docker compose ps` |
| La aplicación dice "no responde (¿está levantado el contenedor?)" | Ese contenedor está caído: `docker compose up -d` |
| Faltan colecciones de MongoDB | `docker compose exec mongo mongosh /docker-entrypoint-initdb.d/01_schema.js` |
| Un puerto (5432, 6379...) ya está en uso | Hay otro PostgreSQL/Redis instalado en la PC: detenerlo o cambiar el puerto en `.env` y en `docker-compose.yml` |
