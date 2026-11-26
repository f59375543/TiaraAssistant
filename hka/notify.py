from __future__ import annotations

import json
import os
import re
import ssl
import smtplib
import urllib.request
from urllib.parse import urlsplit
from email.message import EmailMessage
from threading import Event
from typing import Any
from .mail_credentials import unprotect_secret


def _webhook_url(item: Any) -> str | None:
    if not isinstance(item, dict) or type(item.get("enabled", True)) is not bool:
        raise ValueError("配置应包含 URL 和布尔开关")
    if not item.get("enabled", True):
        return None
    url = item.get("url")
    if not isinstance(url, str) or any(char.isspace() for char in url):
        raise ValueError("URL 无效")
    address = urlsplit(url)
    if address.scheme not in {"https", "http"} or not address.hostname:
        raise ValueError("URL 无效")
    return url


def validate_notifications(settings: Any) -> None:
    if not isinstance(settings, dict):
        raise ValueError("notifications 必须是对象")
    for flag in ("on_success", "on_error"):
        if type(settings.get(flag, True)) is not bool:
            raise ValueError(f"notifications.{flag} 必须是布尔值")
    hooks = settings.get("webhooks", [])
    if not isinstance(hooks, list):
        raise ValueError("notifications.webhooks 必须是数组")
    for index, item in enumerate(hooks, 1):
        try:
            _webhook_url(item)
        except ValueError as exc:
            raise ValueError(f"Webhook {index} 配置无效") from exc
    email = settings.get("email", {})
    if not isinstance(email, dict) or type(email.get("enabled", False)) is not bool:
        raise ValueError("notifications.email 必须是带布尔开关的对象")
    if email.get("enabled"):
        email_options(email)


def email_options(settings: dict) -> dict:
    email = dict(settings)
    provider = email.get('provider') or ('qq' if email.get('qq_number') or not email.get('host') else 'smtp')
    if provider == 'qq':
        number = str(email.get('qq_number', '')).strip()
        if not re.fullmatch(r'[1-9][0-9]{4,14}', number):
            raise ValueError('请填写有效 QQ 号（SMTP 邮件通知）')
        address = number + '@qq.com'
        email.update(host='smtp.qq.com', port=465, security='ssl',
                     username=address, sender=address, recipient=address)
    return email


def send_email(settings: dict, *, title: str, body: str) -> None:
    email = email_options(settings)
    encrypted = email.get('authorization_code_encrypted', '')
    secret = unprotect_secret(encrypted) if encrypted else os.getenv(email.get('password_env', 'HKA_SMTP_PASSWORD'), '')
    if not secret:
        raise ValueError('请填写 SMTP 授权码，或设置 SMTP 密码环境变量')
    message = EmailMessage()
    message['Subject'] = title
    message['From'] = email.get('sender', '')
    message['To'] = email.get('recipient', '')
    message.set_content(body)
    context = ssl.create_default_context()
    secure = email.get('security', 'ssl' if int(email.get('port', 587)) == 465 else 'starttls') == 'ssl'
    try:
        if secure:
            client = smtplib.SMTP_SSL(email['host'], int(email.get('port', 465)), timeout=10, context=context)
        else:
            client = smtplib.SMTP(email['host'], int(email.get('port', 587)), timeout=10)
        with client as smtp:
            if not secure:
                smtp.starttls(context=context)
            smtp.login(email['username'], secret)
            smtp.send_message(message)
    except Exception as exc:
        raise ValueError(str(exc).replace(secret, '[已隐藏]')) from None


def send_notifications(config: dict[str, Any], *, success: bool, title: str, body: str,
                       cancel: Event | None = None) -> list[str]:
    settings = config.get("notifications", {})
    if not isinstance(settings, dict):
        return ["notifications 必须是对象"]
    flag = "on_success" if success else "on_error"
    enabled = settings.get(flag, True)
    if type(enabled) is not bool:
        return [f"notifications.{flag} 必须是布尔值，本次通知已跳过"]
    if not enabled:
        return []
    errors: list[str] = []
    hooks = settings.get("webhooks", [])
    if not isinstance(hooks, list):
        errors.append("Webhook 配置应为数组")
        hooks = []
    for index, item in enumerate(hooks):
        if cancel is not None and cancel.is_set():
            errors.append("运行已停止，剩余通知已跳过")
            return errors
        try:
            url = _webhook_url(item)
            if url is None:
                continue
            payload = json.dumps({"title": title, "content": body, "success": success}, ensure_ascii=False).encode("utf-8")
            request = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json; charset=utf-8"}, method="POST")
            with urllib.request.urlopen(request, timeout=10) as response:
                if response.status >= 400:
                    raise RuntimeError(f"HTTP {response.status}")
        except Exception as exc:
            errors.append(f"Webhook {index + 1}: {exc}")
    email = settings.get("email", {})
    if not isinstance(email, dict):
        errors.append("邮件通知配置应为对象")
        return errors
    if type(email.get("enabled", False)) is not bool:
        errors.append("notifications.email.enabled 必须是布尔值，邮件通知已跳过")
    elif email.get("enabled"):
        if cancel is not None and cancel.is_set():
            errors.append("运行已停止，邮件通知已跳过")
            return errors
        try:
            send_email(email, title=title, body=body)
        except Exception as exc:
            errors.append(f"邮件: {exc}")
    return errors
