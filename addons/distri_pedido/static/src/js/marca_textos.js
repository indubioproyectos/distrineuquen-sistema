/** 19.0.1.129.17: every translated text of the web client and of the
 * website/portal pages («Se bloquearon las notificaciones push de Odoo»,
 * «Instale Odoo», «Campo de Odoo»...) says «DistriNeuquén». The brand goes in
 * where a translation is read (TranslatedString.valueOf, used by _t and by the
 * Owl templates), so the core texts need no copy. Links («Odoo.com») and HTML
 * (markup) stay as they are. Loaded in web.assets_backend and
 * web.assets_frontend. */
import { markup } from "@odoo/owl";
import { TranslatedString } from "@web/core/l10n/translation";
import { patch } from "@web/core/utils/patch";

export const BRAND = "DistriNeuquén";
const MarkupClass = markup("").constructor;

/** A plain text with «Odoo» as «DistriNeuquén». HTML (markup) is left as is. */
export function withBrand(text) {
    if (!text || text instanceof MarkupClass || !String(text).includes("Odoo")) {
        return text;
    }
    return String(text).replace(/\bOdoo\b(?!\.?\w)/g, BRAND);
}

patch(TranslatedString.prototype, {
    valueOf() {
        return withBrand(super.valueOf());
    },
});
