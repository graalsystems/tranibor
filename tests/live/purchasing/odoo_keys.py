"""Run by `odoo shell` in the nightly job `purchasing-live` (never imported):
`odoo shell -d ci < odoo_keys.py`.

It creates the dedicated integration user the purchasing server runs as (not an administrator: the rights
`PROFILE.md` lists, no more) and one JSON-2 API key for it, and a second key for the administrator, which
only the test's seeding uses. Odoo offers no API to mint a key, hence the shell. The keys are printed on
stdout as `PURCHASING_LIVE_...=<key>` lines, which the job reads and masks; Odoo's own logs go to stderr.
"""

import datetime

users = env["res.users"]  # noqa: F821 - `env` is what `odoo shell` provides
groups_field = "group_ids" if "group_ids" in users._fields else "groups_id"  # Odoo 19 renamed it
wanted = ("base.group_user", "purchase.group_purchase_user", "base.group_partner_manager")
groups = [env.ref(xmlid).id for xmlid in wanted]  # noqa: F821 - a missing group must fail the job

bot = users.create(
    {"name": "Purchasing integration", "login": "purchasing-bot", "email": "purchasing-bot@example.test"}
)
bot.write({groups_field: [(6, 0, groups)]})

expires = datetime.datetime.now() + datetime.timedelta(days=1)


def key_of(user):
    return env["res.users.apikeys"].with_user(user)._generate("rpc", "purchasing-live", expires)  # noqa: F821


print("PURCHASING_LIVE_ODOO_ADMIN_KEY=" + key_of(env.ref("base.user_admin")))  # noqa: F821
print("PURCHASING_LIVE_INTEGRATION_KEY=" + key_of(bot))
env.cr.commit()  # noqa: F821
