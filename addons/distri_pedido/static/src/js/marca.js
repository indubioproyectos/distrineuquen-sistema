/** 19.0.1.129.4: no user-visible «Odoo» in the web client; the system is
 * «DistriNeuquén» (server side: views/marca.xml, models/marca.py).
 * - tab title: «DistriNeuquén» when no action names the page (core: «Odoo»);
 * - user menu: without Help/Support, Shortcuts and «My Odoo.com Account»
 *   (they link to odoo.com); Preferences, Install app and Log out stay;
 * - error dialogs: titles without «Odoo» («Odoo Server Error» → «Error de
 *   servidor»), session expired in Spanish (marca.xml);
 * - notifications (toasts): «Odoo» → «DistriNeuquén» («Odoo will send
 *   notifications on this device»);
 * - Discuss: «Install Odoo» prompt → «Instalar DistriNeuquén»;
 * - Settings: the edition block without the Odoo name (marca.xml). */
import { markup } from "@odoo/owl";
import { MessagingMenu } from "@mail/core/public_web/messaging_menu";
import {
    ClientErrorDialog,
    ErrorDialog,
    NetworkErrorDialog,
    RedirectWarningDialog,
    RPCErrorDialog,
    WarningDialog,
} from "@web/core/errors/error_dialogs";
import { notificationService } from "@web/core/notifications/notification_service";
import { registry } from "@web/core/registry";
import { patch } from "@web/core/utils/patch";

export const BRAND = "DistriNeuquén";
const MarkupClass = markup("").constructor;

/** A dialog title without the Odoo name, in English or Spanish:
 * «Odoo Server Error» → «Server Error», «Error de servidor de Odoo» →
 * «Error de servidor», «Expiró la sesión de Odoo» → «Expiró la sesión». */
export function titleWithoutOdoo(title) {
    if (!title || !String(title).includes("Odoo")) {
        return title;
    }
    return String(title)
        .replace(/\s+(?:de|del)\s+Odoo\b/g, "")
        .replace(/\bOdoo\s+/g, "")
        .replace(/\bOdoo\b/g, BRAND)
        .trim();
}

/** A plain text with «Odoo» as «DistriNeuquén». HTML (markup) is left as is. */
export function withBrand(text) {
    if (!text || text instanceof MarkupClass || !String(text).includes("Odoo")) {
        return text;
    }
    return String(text).replace(/\bOdoo\b(?!\.?\w)/g, BRAND);
}

// --- tab title -------------------------------------------------------------
// Same service as core (web/core/browser/title_service.js), other fallback.
export const distriTitleService = {
    start() {
        const titleCounters = {};
        const titleParts = {};

        function updateTitle() {
            const counter = Object.values(titleCounters).reduce((acc, count) => acc + count, 0);
            const name = Object.values(titleParts).join(" - ") || BRAND;
            document.title = counter ? `(${counter}) ${name}` : name;
        }

        function apply(target, values) {
            for (const key in values) {
                if (values[key]) {
                    target[key] = values[key];
                } else {
                    delete target[key];
                }
            }
            updateTitle();
        }

        return {
            get current() {
                return document.title;
            },
            getParts() {
                return Object.assign({}, titleParts);
            },
            setCounters(counters) {
                apply(titleCounters, counters);
            },
            setParts(parts) {
                apply(titleParts, parts);
            },
        };
    },
};
registry.category("services").add("title", distriTitleService, { force: true });

// --- user menu -------------------------------------------------------------
export const REMOVED_USER_MENU_ITEMS = ["support", "shortcuts", "separator", "odoo_account"];
const userMenuItems = registry.category("user_menuitems");
for (const key of REMOVED_USER_MENU_ITEMS) {
    if (userMenuItems.contains(key)) {
        userMenuItems.remove(key);
    }
}

// --- error dialogs ---------------------------------------------------------
ErrorDialog.title = "Error";
ClientErrorDialog.title = "Error en la pantalla";
NetworkErrorDialog.title = "Error de conexión";

patch(RPCErrorDialog.prototype, {
    inferTitle() {
        super.inferTitle();
        if (this.title) {
            this.title = titleWithoutOdoo(this.title);
        }
    },
});

patch(WarningDialog.prototype, {
    inferTitle() {
        return titleWithoutOdoo(super.inferTitle());
    },
});

patch(RedirectWarningDialog.prototype, {
    setup() {
        super.setup();
        this.title = titleWithoutOdoo(this.title);
    },
});

// --- notifications ---------------------------------------------------------
patch(notificationService, {
    start() {
        const api = super.start(...arguments);
        const add = api.add;
        api.add = (message, options = {}) => {
            const opts = options.title ? { ...options, title: withBrand(options.title) } : options;
            return add(withBrand(message), opts);
        };
        return api;
    },
});

// --- Discuss: «Install Odoo» -----------------------------------------------
patch(MessagingMenu.prototype, {
    get installationRequest() {
        return { ...super.installationRequest, displayName: `Instalar ${BRAND}` };
    },
});
