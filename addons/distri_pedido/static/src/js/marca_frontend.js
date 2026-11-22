/** 19.0.1.129.4: the website/portal «session expired» notice without
 * «Odoo» (core: web/static/src/public/error_notifications.js). */
import { registry } from "@web/core/registry";

const sessionExpired = {
    title: "Sesión vencida",
    message: "Tu sesión venció. La página se va a recargar.",
    buttons: [{ text: "Aceptar", click: () => window.location.reload(true), close: true }],
};

registry
    .category("error_notifications")
    .add("odoo.http.SessionExpiredException", sessionExpired, { force: true })
    .add("werkzeug.exceptions.Forbidden", sessionExpired, { force: true });
