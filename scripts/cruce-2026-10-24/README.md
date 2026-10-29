# Carga del cruce del 24/10/2026 en el lab

Carga en `distri_lab` los cobros verificados en el cruce del 24/10. Los datos de clientes quedan en `/srv/docs/distrineuquen-modulo/insumos/` y nunca entran a este repositorio.

Cada paso corre en **prueba** (deshace todo al final) salvo con `DO_COMMIT=1`. El registro de cada corrida (ids creados y líneas RESULT) queda en `insumos/cruce-2026-10-24/carga-lab/` con permisos 600.

## Orden

| Paso | Comando | Qué hace |
|---|---|---|
| 1 | `paso1-limpiar.sh` | Vacía el tablero de cobros: reportes, ingresos, egresos, cierres y mensajes de WhatsApp. Conserva la historia de Online y los clientes. Se detiene si algún cobro ya tiene pago. |
| 2 | `paso2-sincronizar.sh` | Lectura de Online (sin escribir en Online): clientes nuevos, historia de cada cliente y copia de los pagos. |
| 3 | `paso3-cargar-mp.sh` | Importa los reportes originales de Mercado Pago del 18/03 al 24/10. Confirma los cobros «cargar» con su cliente, el comprobante adjunto y una nota. Cierra los «ya en Online» y deja una nota en los «revisar». |
| 4 | `paso4-cargar-efectivo.sh` | Registra en el diario Efectivo los 140 cobros de las planillas y el efectivo avisado por WhatsApp que no está en Online. |
| 6 | `paso6-correcciones.sh` | Correcciones de la auditoría del 24/10: anula los pagos que ya estaban en Online, pasa dos pagos al cliente correcto, cierra las tarjetas que el cruce da por cargadas en Online, reabre las «revisar» que el sistema unió solo por importe y borra las copias de los borradores eliminados de Online. Cada cambio deja una nota. |

Para cada paso: primero la prueba, después la carga.

```
scripts/cruce-2026-10-24/paso1-limpiar.sh
DO_COMMIT=1 scripts/cruce-2026-10-24/paso1-limpiar.sh
```

Los pasos 3 y 4 necesitan los pasos 1 y 2 ya escritos. Para probar el paso 3 antes de limpiar: `SIMULAR_LIMPIEZA=1 scripts/cruce-2026-10-24/paso3-cargar-mp.sh` (limpia y carga en la misma transacción y deshace todo).

## Qué controla

- Cada cliente se busca por su id de Online (`ref` = `ODOO-<id>`). Si falta uno, no se escribe nada.
- La cantidad de pagos nuevos es igual a la cantidad de cobros cargados, y su total es igual al importe del cruce.
- El saldo de cada cliente baja exactamente lo cargado: el pago cuenta una sola vez frente a la historia de Online.
- Volver a correr un paso no duplica nada.
- Lo que el sistema ya había vinculado a un pago de Online, apartado como mayorista o marcado con devolución no se confirma: se lista como `SKIP` para revisarlo a mano.
