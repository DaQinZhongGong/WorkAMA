# -*- coding: utf-8 -*-
"""对 delivery.py 做定向替换以加入真实 SMTP/Webhook 投递。运行后自删。"""
import io
import os
import sys

PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "delivery.py")
with io.open(PATH, "r", encoding="utf-8") as f:
    src = f.read()

original = src


def rep(old, new, label):
    global src
    if old not in src:
        sys.exit("FAIL " + label)
    src = src.replace(old, new, 1)


# R1: imports
rep(
    "import smtplib\nfrom datetime import UTC, datetime\nfrom email.message import EmailMessage\nfrom typing import Any\n\nfrom workama_platform.core import pool, settings",
    "import smtplib\nfrom datetime import UTC, datetime\nfrom email.message import EmailMessage\nfrom email.mime.multipart import MIMEMultipart\nfrom email.mime.text import MIMEText\nfrom typing import Any\n\nimport aiosmtplib\nimport httpx\n\nfrom workama_platform.core import pool, settings",
    "R1 imports",
)

print("OK")
