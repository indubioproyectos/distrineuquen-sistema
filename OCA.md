# Módulos OCA del lab

`oca-addons/` no está en este repositorio: son copias sin modificar de módulos de la OCA
(Odoo Community Association), montadas en `/mnt/oca-addons`. Se usaron para evaluar la
conciliación bancaria (25/09/2026). Para reconstruirlos, bajar estas versiones exactas de la
rama `19.0` de cada repositorio:

| Módulo | Repositorio OCA | Versión del manifiesto |
|---|---|---|
| `account_reconcile_oca` | `OCA/account-reconcile` | 19.0.1.0.10 |
| `account_statement_base` | `OCA/account-reconcile` | 19.0.1.0.1 |
| `account_statement_import_base` | `OCA/bank-statement-import` | 19.0.1.0.0 |
| `account_statement_import_file` | `OCA/bank-statement-import` | 19.0.1.0.0 |
| `account_statement_import_file_reconcile_oca` | `OCA/bank-statement-import` | 19.0.1.0.0 |
| `account_statement_import_sheet_file` | `OCA/bank-statement-import` | 19.0.2.0.2 |

El repositorio sale del `README.rst` de cada carpeta (enlaces a `github.com/OCA/...`,
verificado el 22/10/2026) y la versión de su `__manifest__.py`. `distri_pedido` no depende de
ninguno.
