# Swap: plan contable argentino (Monotributo) + remito para el cliente

Pasa el lab vivo (`distri_lab`, plan genérico) a la configuración de Odoo Online
(ar_base, Responsable Monotributo) con los cambios del módulo que hacen que el
cliente vea el remito y nunca la factura. **Solo con la aprobación de ale.**

## Qué queda configurado

| | Odoo Online (26/09/2026) | Lab después del swap |
|---|---|---|
| Plan contable | ar_base | ar_base |
| Responsabilidad | Responsable Monotributo | Responsable Monotributo |
| Diario de ventas | Ventas Preimpreso, 0001, sin documentos, PdV 1 | igual |
| Numeración | 0001/AAAA/NNNNN, NC en la misma serie | igual; sigue en 0001/2026/06276 |
| IVA | 0 en todas | sin impuestos en productos, pedidos ni facturas |
| ARCA / CUIT / IIBB | no | no |
| Cobro en ruta | — | diario Efectivo (CSH1); transferencias en Banco (BNK1) |

## Archivos

- `lab.sh`: corre un Odoo aparte (contenedor descartable) sobre la copia. Nunca
  toca `odoo-lab-odoo-1`, `odoo.conf` ni `addons/`.
- `rebuild.sh <dump> <filestore.tgz>`: arma `distri_lab_ar` desde un dump:
  restaura, instala l10n_ar, traducciones es_ES (`i18n.py`), plan ar_base y
  diarios (`convert.py`), actualiza `distri_pedido` con `addons-ar-dev/` y
  compara saldos (`snap.py` + `compare.py`). `RUN_TESTS=1` corre los tests.
- `verify_flow.sh`: en un clon descartable prueba factura sola al armar,
  rendición a Efectivo con nota de crédito y revisa la página de cada cliente.
- `swap_day.sh --approved-by-ale`: el día del swap, pasos 0 a 7.
- `distri_pedido-remito.patch`: los cambios del módulo.
- `limpieza.py`: borra los datos de prueba del lab (26/09/2026, ver abajo).

El swap ya se hizo (26/09/2026, `swap_finish.sh`). `addons-ar-base/` y
`addons-ar-dev/` se borraron después: el módulo de antes del swap está en
`/srv/backups/distri_pedido-20260926-2246-pre-swap.tgz` (es lo que usa la
vuelta atrás) y `lab.sh` usa ahora `/srv/odoo-lab/addons`. `swap_day.sh` y
`rebuild.sh` quedan como registro; para volver a usarlos hay que recrear esas
carpetas.

## Pasos del día (swap_day.sh)

0. Chequea que `addons/distri_pedido` siga igual a `addons-ar-base/`. Si alguien
   lo cambió, se aborta: primero se reaplica el patch sobre el módulo actual.
1. `docker compose stop odoo`. **Empieza el corte.**
2. Backups: dump de `distri_lab`, filestore y el módulo vivo, en `/srv/backups`.
3. `rebuild.sh` con ese dump (unos 80 s; con tests, 2 min 15 s).
4. Copia `addons-ar-dev/distri_pedido` sobre `addons/distri_pedido`. La base
   nueva ya quedó actualizada con esos mismos archivos: no hace falta `-u`.
5. Renombra `distri_lab` a `distri_lab_generic_<fecha>` y `distri_lab_ar` a
   `distri_lab`, y lo mismo con las carpetas del filestore.
6. `docker compose start odoo`. **Termina el corte.** Prueba `/web/login` y `/pedido`.
7. Compara saldos e historial de la base nueva contra la foto previa.

Corte estimado: **3 a 5 minutos** (medido: restauración 25 s, conversión 50 s,
actualización del módulo 11 s, arranque de unos segundos). Mercado Pago y WhatsApp
reintentan los avisos que lleguen en ese rato.

## Vuelta atrás (unos 2 minutos)

```bash
cd /srv/odoo-lab && docker compose stop odoo
docker exec -i odoo-lab-db-1 psql -U odoo -d postgres \
  -c "alter database distri_lab rename to distri_lab_ar_fallido" \
  -c "alter database distri_lab_generic_<fecha> rename to distri_lab"
docker run --rm -v odoo-lab_odoo-data:/var/lib/odoo odoo:19 bash -c \
  "cd /var/lib/odoo/filestore && mv distri_lab distri_lab_ar_fallido && mv distri_lab_generic_<fecha> distri_lab"
rm -rf addons/distri_pedido && tar xzf /srv/backups/distri_pedido-<fecha>-pre-swap.tgz -C addons
docker compose start odoo
```

Si la base vieja no estuviera, se restaura el dump `distri_lab-<fecha>-swap.dump`.

## Limpieza (26/09/2026, `limpieza.py`)

Se borraron las facturas 0001/2026/06276 a 06280, los pagos PBNK1/2026/00001
y 00002, los pedidos S00145 y S00146 (con sus entregas; el stock volvió), la
ruta de prueba del 26/09 y los usuarios armador.prueba.factura y
revision.temporal. La historia importada quedó igual (21.011 movimientos, md5
60a054d978ea30066419000d3c39e005).

## Numeración

- Remitos: `R-NNNNN` (antes `WH/OUT/NNNNN`), migración 19.0.1.3.0.
- Facturas: el parámetro `distri_pedido.factura_ultimo_numero_online`
  (`0001/2026/06275`) es un piso: la próxima factura del diario de ventas sale
  en el siguiente número aunque no haya ninguna en Odoo
  (`models/factura_numeracion.py`).

- Las facturas del lab siguen la serie de Online (`NEXT_NUMBER`, hoy
  `0001/2026/06276`). El día que el lab reemplace de verdad a Online se fija
  en el último número de Online + 1, y se reimporta el historial hasta ese día.
- El saldo descarta un documento repetido solo si coinciden el número **y** el
  importe (`models/cuenta.py`, `clave_documento`). Los números propios de Odoo
  (`PBNK1/2026/00001`, `PCSH1/2026/00001`) ya se repiten con otros de Online, y
  antes eso podía esconder un pago del historial del mismo cliente.
