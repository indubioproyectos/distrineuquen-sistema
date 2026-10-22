/**
 * Este archivo no hace nada a propósito.
 *
 * Existe para cambiar el contenido del paquete `web.assets_frontend_lazy` y,
 * con él, la URL que Odoo le pone. Cloudflare tenía un 502 cacheado sobre la
 * URL anterior y por eso la pantalla de login no mostraba el formulario: el
 * JavaScript que lo revela nunca llegaba a cargarse.
 *
 * Es una solución de rodeo, no la de fondo. La de fondo es purgar la caché de
 * Cloudflare, que se hace desde su panel. Si el 502 vuelve a aparecer sobre la
 * URL nueva, no alcanza con volver a tocar este archivo: hay que purgar.
 */
