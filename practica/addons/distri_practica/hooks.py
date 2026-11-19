import secrets

from .controllers.main import practice_users


def post_init_hook(env):
    """The practice users get a long random password nobody knows (they only
    enter through the landing buttons) and the language of the company's
    administrator."""
    admin = env.ref("base.user_admin", raise_if_not_found=False)
    lang = admin.with_context(active_test=False).partner_id.lang if admin else False
    for user in practice_users(env):
        vals = {"password": secrets.token_urlsafe(48)}
        if lang:
            vals["lang"] = lang
        user.sudo().write(vals)
