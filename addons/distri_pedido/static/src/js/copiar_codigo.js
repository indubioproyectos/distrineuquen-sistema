/** «Copiar código» (19.0.1.103.0, models/pegar_numero.py): the order's
 * WhatsApp line («Pedido S00164 · código 7K3P») with a button that copies
 * it, to search it in WhatsApp Web. Clipboard API first; without it (no
 * https, permission refused) the old execCommand copy; when that fails too
 * the text is left selected on screen with a hint to copy it by hand. The
 * text itself selects whole with one click (user-select-all). */
import { Component, useRef, useState } from "@odoo/owl";
import { browser } from "@web/core/browser/browser";
import { registry } from "@web/core/registry";
import { standardFieldProps } from "@web/views/fields/standard_field_props";

export class DistriCopiarField extends Component {
    static template = "distri_pedido.DistriCopiarField";
    static props = { ...standardFieldProps };

    setup() {
        this.texto = useRef("texto");
        this.state = useState({ msg: "", ok: false });
    }

    get value() {
        return this.props.record.data[this.props.name] || "";
    }

    copiarViejo(text) {
        const area = document.createElement("textarea");
        area.value = text;
        area.setAttribute("readonly", "");
        area.style.position = "fixed";
        area.style.opacity = "0";
        document.body.appendChild(area);
        area.select();
        let ok = false;
        try {
            ok = document.execCommand("copy");
        } catch {
            ok = false;
        }
        document.body.removeChild(area);
        return ok;
    }

    seleccionar() {
        const el = this.texto.el;
        if (!el) {
            return;
        }
        const range = document.createRange();
        range.selectNodeContents(el);
        const selection = window.getSelection();
        selection.removeAllRanges();
        selection.addRange(range);
    }

    async copiar() {
        const text = this.value;
        if (!text) {
            return;
        }
        let ok = false;
        const clipboard = browser.navigator.clipboard;
        if (clipboard && window.isSecureContext) {
            try {
                await clipboard.writeText(text);
                ok = true;
            } catch {
                ok = false;
            }
        }
        if (!ok) {
            ok = this.copiarViejo(text);
        }
        if (!ok) {
            this.seleccionar();
        }
        this.state.ok = ok;
        this.state.msg = ok ? "Copiado" : "Quedó marcado: copialo con Ctrl+C";
        browser.clearTimeout(this.timer);
        this.timer = browser.setTimeout(() => {
            this.state.msg = "";
        }, ok ? 2000 : 8000);
    }
}

export const distriCopiarField = {
    component: DistriCopiarField,
    displayName: "Copiar código",
    supportedTypes: ["char"],
};

registry.category("fields").add("distri_copiar", distriCopiarField);
