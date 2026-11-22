"""19.0.1.129.4: the project records (data/proyecto.xml) are noupdate, so
their texts are renamed here like the file: «Odoo Online» is «el sistema
anterior» and this Odoo is «el sistema» or «DistriNeuquén». The same rules
apply to a text the owner edited since; a text without «Odoo» is untouched."""
import logging
import re

_logger = logging.getLogger(__name__)

FIELDS = ("name", "description", "next_step")

EXACT = [
    ("Odoo Online (la versión alquilada de hoy) contra un Odoo Community propio",
     "El sistema anterior (la versión alquilada de hoy) contra un sistema propio"),
    ("Qué cuesta hoy Odoo Online, qué cuesta un Odoo Community propio",
     "Qué cuesta hoy el sistema anterior, qué cuesta un sistema propio"),
    ("Odoo Community es de código abierto: se puede modificar.",
     "La base del sistema propio es de código abierto: se puede modificar."),
    ("Hoy el sistema corre en Odoo Online, la versión alquilada.",
     "Hoy todo corre en el sistema anterior, la versión alquilada."),
    ("Odoo Community, propio", "Sistema propio"),
    ("Con Odoo Community", "Con el sistema propio"),
    ("Odoo Online (hoy)", "Sistema anterior (hoy)"),
    ("Las copias y las actualizaciones las maneja Odoo.",
     "Las copias y las actualizaciones las maneja el proveedor del sistema anterior."),
    ("algunas tareas que hoy hace Odoo pasan", "algunas tareas que hoy hace el proveedor pasan"),
    ("actualizaciones de Odoo y atención", "actualizaciones del sistema y atención"),
    ("Por qué conviene un Odoo propio", "Por qué conviene un sistema propio"),
    ("El Odoo actual", "El sistema anterior"),
    ("la versión alquilada de Odoo", "la versión alquilada del sistema anterior"),
    ("¿Por qué no conectar AgilPedido con Odoo?", "¿Por qué no conectar AgilPedido con el sistema?"),
    ("como antes de Odoo.", "como antes del sistema nuevo."),
    ("en un entorno independiente de Odoo", "en un entorno independiente del sistema"),
    ("Odoo › Ventas", "Ventas"),
    ("Odoo real", "sistema anterior"),
    ("Odoo de produccion", "sistema anterior"),
]

GENERIC = [
    (r"\bde(\s+)Odoo Online\b", r"del\1sistema anterior"),
    (r"\bDe(\s+)Odoo Online\b", r"Del\1sistema anterior"),
    (r"\ba(\s+)Odoo Online\b", r"al\1sistema anterior"),
    (r"\b([Ee]n|[Pp]ara|[Cc]on|que|[Pp]or)(\s+)Odoo Online\b", r"\1\2el sistema anterior"),
    (r"\(Odoo Online\)", "(sistema anterior)"),
    (r"(^\s*|[.¿¡«(]\s*)Odoo Online\b", r"\1Sistema anterior"),
    (r"\bOdoo Online\b", "el sistema anterior"),
    (r"\b(un|el|al|del)(\s+)Odoo(\s+)(propio|Community)\b", r"\1\2sistema\3propio"),
    (r"\bOdoo(\s+)(propio|Community)\b", r"sistema\1propio"),
    (r"\beste(\s+)Odoo\b", r"este\1sistema"),
    (r"\b([Dd])e(\s+)Odoo\b", r"\1el\2sistema"),
    (r"\b([Aa])(\s+)Odoo\b", r"\1l\2sistema"),
    (r"\b([Ee]n|[Pp]or|[Cc]on|[Dd]esde|[Ss]in|[Hh]acia|[Hh]asta)(\s+)Odoo\b", r"\1\2el sistema"),
    (r"\b(Total|Subtotal)(\s+)Odoo\b", r"\1\2sistema"),
    (r"(^\s*|[.¿¡↓]\s+)Odoo\b(?=\s+\w)", r"\1El sistema"),
    (r"(,\s+|\by\s+|\bque\s+|\b[Aa]hí\s+|\bjunto\s+|:\s+|\bminutos\s+|\blo prepara\s+)Odoo\b", r"\1el sistema"),
    (r"\(Odoo\)", "(sistema)"),
    (r"\bOdoo\b(?!\.com|Bot)", "DistriNeuquén"),
]


def without_odoo(text):
    """`text` with the user-visible «Odoo» renamed (exact phrases first)."""
    if not text or "Odoo" not in text:
        return text
    for old, new in EXACT:
        text = text.replace(old, new)
    for pattern, repl in GENERIC:
        text = re.sub(pattern, repl, text, flags=re.M)
    return text


def migrate(cr, version):
    if not version:
        return
    cr.execute("""
        SELECT p.id, p.name, p.description, p.next_step
          FROM distri_project_module p
          JOIN ir_model_data d ON d.model = 'distri.project.module' AND d.res_id = p.id
         WHERE d.module = 'distri_pedido'""")
    renamed = []
    for row in cr.fetchall():
        values = dict(zip(FIELDS, row[1:]))
        new = {field: without_odoo(value) for field, value in values.items()}
        changes = {field: value for field, value in new.items() if value != values[field]}
        if not changes:
            continue
        sets = ", ".join("%s = %%s" % field for field in changes)
        cr.execute("UPDATE distri_project_module SET " + sets + " WHERE id = %s",
                   [*changes.values(), row[0]])
        renamed.append(row[0])
    _logger.info("branding: %s project records without Odoo: %s", len(renamed), renamed)
