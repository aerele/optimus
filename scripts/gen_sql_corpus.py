# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Regenerate ``optimus/tests/data/frappe_sql_corpus.json``: statements rendered by Frappe's own
query builder (pypika, MariaDB and PostgreSQL builders) and by pymysql's ``mogrify``, each with
the secret it carries. Needs a bench interpreter, not the CI venv:

    cd <bench>/sites && ../env/bin/python ../apps/optimus/scripts/gen_sql_corpus.py <site>

The query shapes are the ones Frappe writes: ``set_encrypted_password``, ``get_decrypted_password``,
``bulk_update`` (CASE), ``set_value``, ``Singles`` rows, ``__Auth`` rows, Email Queue and Webhook
Request Log rows. The unit tests read the JSON only, so CI needs no Frappe.
"""

import json
import sys
from pathlib import Path

import frappe
import pymysql
from frappe.query_builder.functions import Count
from frappe.query_builder.utils import get_query_builder
from pypika.terms import Case

OUT = Path(__file__).resolve().parent.parent / "optimus" / "tests" / "data" / "frappe_sql_corpus.json"
SECRET = "Zq9xK2vLm7Wp"
HASH = "$pbkdf2-sha256$29000$" + SECRET + "abcdef"
FORM = f"password={SECRET}&client_secret={SECRET}b&user=a%40b.c"
URL = f"https://x.example/cb?api_key={SECRET}&state=1"
HEADERS = json.dumps({"X-API-Key": SECRET, "Authorization": f"Bearer {SECRET}", "Accept": "text/html"})


def mogrify(query, values):
	"""The statement as pymysql sends it for ``frappe.db.sql(query, values)``."""
	connection = pymysql.connections.Connection(defer_connect=True)
	connection.server_status = 0  # as after a connect: backslash escapes on
	return connection.cursor().mogrify(query, values)


def builders(qb):
	"""``(label, statement, secrets)`` of one query builder."""
	user, auth, singles = qb.DocType("User"), qb.Table("__Auth"), qb.Table("tabSingles")
	queue, hook = qb.DocType("Email Queue"), qb.DocType("Webhook Request Log")
	task = qb.DocType("Task")
	case = Case()
	for name, status in (("T-1", SECRET), ("T-2", "Open")):
		case.when(task.name == name, status)
	return [
		(
			"get_decrypted_password",
			qb.from_(auth)
			.select(auth.password)
			.where(
				(auth.doctype == "User")
				& (auth.name == "a@b.c")
				& (auth.fieldname == "password")
				& (auth.encrypted == 1)
			)
			.limit(1),
			[],
		),
		(
			"set_encrypted_password",
			qb.into(auth)
			.columns(auth.doctype, auth.name, auth.fieldname, auth.password, auth.encrypted)
			.insert("User", "a@b.c", "password", HASH, 1),
			[HASH],
		),
		("password_filter", qb.from_(user).select(user.name).where(user.password == SECRET), [SECRET]),
		(
			"api_key_in",
			qb.from_(user).select(user.name).where(user.api_key.isin([SECRET, SECRET + "2"])),
			[SECRET],
		),
		("set_value", qb.update(user).set(user.api_secret, SECRET).where(user.name == "a@b.c"), [SECRET]),
		(
			"bulk_update_case",
			qb.update(task).set(task.api_key, case.else_(task.api_key)).where(task.name.isin(["T-1", "T-2"])),
			[SECRET],
		),
		(
			"single_value",
			qb.update(singles)
			.set(singles.value, SECRET)
			.where((singles.doctype == "Razorpay Settings") & (singles.field == "api_key")),
			[SECRET],
		),
		(
			"single_insert",
			qb.into(singles)
			.columns("doctype", "field", "value")
			.insert("Razorpay Settings", "api_secret", SECRET),
			[SECRET],
		),
		(
			"email_queue_form_body",
			qb.update(queue).set(queue.message, FORM).where(queue.name == "EQ-1"),
			[SECRET, SECRET + "b"],
		),
		("email_queue_url", qb.update(queue).set(queue.message, URL).where(queue.name == "EQ-1"), [SECRET]),
		(
			"webhook_log_headers",
			qb.update(hook).set(hook.headers, HEADERS).where(hook.name == "WRL-1"),
			[SECRET],
		),
		(
			"count_with_token_filter",
			qb.from_(user).select(Count("*")).where(user.reset_password_key == SECRET),
			[SECRET],
		),
		("hash_filter", qb.from_(user).select(user.name).where(user.password.like(f"%{HASH}")), [HASH]),
	]


def main(site):
	frappe.init(site, sites_path=".")
	rows = []
	for dialect, db_type in (("mariadb", "mariadb"), ("postgres", "postgres")):
		for label, query, secrets in builders(get_query_builder(db_type)):
			rows.append({"dialect": dialect, "source": f"qb:{label}", "sql": str(query), "secrets": secrets})
	user_sql = (
		"SELECT `name` FROM `tabUser` WHERE `password` = %s AND `token` = %(token)s AND `name` = %(name)s"
	)
	rows.append(
		{
			"dialect": "mariadb",
			"source": "mogrify:named",
			"sql": mogrify(
				user_sql.replace("%s", "%(password)s"),
				{"password": SECRET, "token": SECRET + "t", "name": "x"},
			),
			"secrets": [SECRET, SECRET + "t"],
		}
	)
	rows.append(
		{
			"dialect": "mariadb",
			"source": "mogrify:positional",
			"sql": mogrify(
				"UPDATE `tabUser` SET `api_secret` = %s, `last_name` = %s WHERE `name` = %s",
				(SECRET, "O'Brien", "a@b.c"),
			),
			"secrets": [SECRET],
		}
	)
	rows.append(
		{
			"dialect": "mariadb",
			"source": "mogrify:quote_escapes",
			"sql": mogrify(
				"UPDATE `tabUser` SET `password` = %s WHERE `name` = %s", ("p'a\"s\\s" + SECRET, "a")
			),
			"secrets": [SECRET],
		}
	)
	OUT.write_text(json.dumps(rows, indent="\t", ensure_ascii=False) + "\n")
	print(f"wrote {len(rows)} statements to {OUT}")


if __name__ == "__main__":
	main(sys.argv[1])
