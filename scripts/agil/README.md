# Respaldo diario de la tienda AgilPedido

Todos los días a las 03:30 (hora de Argentina) el servidor guarda una copia de
la tienda pública <https://agilpedido.com/distrineuquen>. Sirve para volver a
cargar la tienda si se vuelve a borrar todo, como pasó un fin de semana de
noviembre de 2026.

- Script: `scripts/agil/respaldo-agil.sh` (usa `respaldo_agil.py`, solo Python
  estándar). Lo corre el crontab del usuario `ale`:
  `30 3 * * * /srv/odoo-lab/scripts/agil/respaldo-agil.sh >/dev/null 2>&1`
- Carpeta: `/srv/odoo-lab/backups-agil/`
- Registro: `/srv/odoo-lab/backups-agil/respaldo-agil.log`
- Copia fuera del servidor: la carpeta entra en el respaldo de cada hora
  (`scripts/respaldo-hora.sh`), que la manda cifrada a Cloudflare R2.

## Límite importante: solo los productos VISIBLES

El respaldo lee la página pública, la misma que ven los clientes. Los
productos **ocultos** en AgilPedido no aparecen ahí, así que **no quedan
guardados**. Si un producto tiene que estar en el respaldo, tiene que estar
visible en la tienda. Tampoco se guardan los pedidos, los clientes ni la
configuración interna de la cuenta (solo los datos públicos de la empresa, en
`datos.json`).

## Qué se guarda

Una carpeta por día, `backups-agil/AAAA-MM-DD/`:

| Archivo | Qué tiene |
|---|---|
| `productos.xlsx` | Los productos con las columnas de la plantilla de importación de AgilPedido: Codigo, Nombre, Descripción, Visible, Precio, Precio salon, Destacado. Listo para importar. |
| `productos.csv` | Cada producto con su sección y el archivo de su foto (la plantilla de importación no tiene columnas de sección ni de foto). |
| `secciones.csv` | Las secciones en el orden de la tienda: nombre, descripción, cantidad de productos y foto. |
| `resumen.txt` | Cuántos productos y secciones había, y qué cambió desde el respaldo anterior (agregados, quitados, cambios de precio). |
| `datos.json` | Todos los datos de la página, completos (empresa, secciones, productos, sucursales, zonas de envío). |
| `tienda.html` | La página tal cual se descargó. |

Las fotos se guardan una sola vez en `backups-agil/fotos/` (el nombre del
archivo es una huella de su contenido, por ejemplo `1f9d…78ec.jpg`). La
columna «Foto» de los CSV dice qué archivo corresponde a cada producto o
sección.

- Precios: en el Excel van en **pesos** (400000 = $ 400.000), que es lo que
  espera el importador de AgilPedido. La página pública los trae en centavos
  y el respaldo ya hace la cuenta.
- Codigo: es el número que AgilPedido le daba a cada producto. Odoo usa ese
  mismo número («AP-n») para reconocer los productos de los pedidos de
  WhatsApp, así que conviene conservarlo al importar.
- Se guardan los últimos 90 días. Las fotos que ya no usa ningún día guardado
  se borran solas.

### Si algo sale mal

Si la página no se puede leer, no trae productos o trae menos de la mitad que
el respaldo anterior (por ejemplo, porque la tienda se borró), **no se pisa
nada**: se crea `AAAA-MM-DD-FALLA/` con la página y un `ALERTA.txt`, y se
agrega una línea a `backups-agil/ALERTA.txt`. El último respaldo bueno nunca
se borra. Conviene mirar `ALERTA.txt` de vez en cuando: si tiene líneas
nuevas, avisar al técnico.

## Cómo volver a cargar la tienda después de un borrado

Lo hacen el dueño y el técnico juntos. Lo que más tiempo lleva es subir las
fotos.

### 1. Elegir el respaldo

1. El técnico entra a `/srv/odoo-lab/backups-agil/` y busca la carpeta con
   fecha **más reciente que no termine en `-FALLA`**. Esa es la última copia
   buena, de antes del borrado.
2. Abrir su `resumen.txt` y confirmar que la cantidad de productos es la
   esperada.
3. El técnico copia a la computadora del dueño esa carpeta y la carpeta
   `fotos/` (por ejemplo, en un .zip).

Si el servidor no estuviera disponible, la misma carpeta se recupera del
respaldo en Cloudflare R2 (el técnico usa `restic restore` con la clave de
`~/.config/indubio/restic-pass`; la ruta dentro del respaldo es
`/srv/odoo-lab/backups-agil/`).

### 2. Volver a crear las secciones

La importación de AgilPedido no crea secciones, así que van a mano y
**primero**.

1. Abrir `secciones.csv` con Excel.
2. En el panel de AgilPedido, crear cada sección con el **Nombre** y la
   **Descripcion** de la planilla, en el orden de la columna **Posicion**.
3. Si la sección tenía imagen, subir el archivo que indica la columna
   **Foto** (está en la carpeta `fotos/`).

### 3. Importar los productos

1. En el panel de AgilPedido, ir a la importación de productos (la misma
   que acepta la planilla modelo con Codigo, Nombre, Descripción, Visible,
   Precio, Precio salon, Destacado).
2. Elegir el archivo `productos.xlsx` del respaldo e importarlo.
3. Revisar dos o tres productos: el precio tiene que coincidir con el de la
   columna **Precio** de `productos.csv` (por ejemplo, 400000 = $ 400.000).
   Si los precios quedaron 100 veces más grandes o más chicos, frenar y
   avisar al técnico antes de seguir.

### 4. Poner cada producto en su sección

La planilla de importación no tiene sección, así que esto va a mano.

1. Abrir `productos.csv` con Excel y ordenarlo por la columna **Seccion**.
2. En AgilPedido, pasar a cada sección los productos que la planilla le
   asigna (buscarlos por **Nombre** o **Codigo**).

### 5. Subir las fotos

1. En `productos.csv`, la columna **Foto** dice el archivo de cada producto,
   por ejemplo `fotos/1f9d…78ec.jpg`.
2. En AgilPedido, abrir cada producto y subir ese archivo desde la carpeta
   `fotos/`. Los productos con la columna Foto vacía no tenían foto.

### 6. Control final

1. Abrir la tienda pública y comparar la cantidad de productos con
   `resumen.txt`.
2. Hacer un pedido de prueba por WhatsApp y ver que llegue bien a Odoo.
3. Al día siguiente, el respaldo nuevo debería decir «Quitados: 0» (o solo
   los que se dejaron afuera a propósito).

## Para el técnico

- Correr a mano: `/srv/odoo-lab/scripts/agil/respaldo-agil.sh`; el resultado
  queda en `respaldo-agil.log`. Código de salida 2 = falla registrada.
- Dos corridas nunca se pisan (`flock` sobre `backups-agil/.lock`); la
  segunda se saltea y lo anota en el registro.
- Pruebas sin tocar los respaldos reales: `AGIL_DESTINO=<carpeta de prueba>`
  y `AGIL_FUENTE=<página guardada>` (lee la página de un archivo local).
- Las fotos se descargan de a 4, con 30 s de tiempo máximo y 2 reintentos.
  Una foto que no baja no hace fallar el respaldo: queda anotada en
  `resumen.txt` y se reintenta al día siguiente. `fotos/indice.json` guarda
  qué foto ya se bajó, para no descargarla todos los días.
- Las direcciones de las fotos en `datos.json` vencen a los 7 días (son
  enlaces firmados); por eso las fotos se guardan como archivos.
