#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""账号体系：邮箱即账号。登记表 users/registry.json.enc（Fernet 加密）存 [{dir,email,created_at,active}]；
目录名 = sha256(email.lower().strip())[:12]，不可逆。
无 FERNET_KEY 时回落明文 users/registry.json 并打印警告（适合 Private 仓库）。
"""
import hashlib
import json
import os
from datetime import date
from pathlib import Path

import workspace as ws

REGISTRY_ENC = ws.USERS_DIR / "registry.json.enc"
REGISTRY_PLAIN = ws.USERS_DIR / "registry.json"

_warned = False

try:
    from cryptography.fernet import Fernet
    _HAS_FERNET = True
except ImportError:
    _HAS_FERNET = False


def hash_email(email):
    return hashlib.sha256(email.lower().strip().encode("utf-8")).hexdigest()[:12]


def _fernet():
    key = os.environ.get("FERNET_KEY", "").strip()
    if key and _HAS_FERNET:
        return Fernet(key.encode() if isinstance(key, str) else key)
    return None


def encrypted_mode():
    """是否启用加密登记表（有 FERNET_KEY 且装了 cryptography）"""
    return _fernet() is not None


def load_registry():
    """读登记表；不存在返回空列表。优先加密体，其次明文"""
    global _warned
    f = _fernet()
    if f and REGISTRY_ENC.exists():
        raw = f.decrypt(REGISTRY_ENC.read_bytes())
        return json.loads(raw.decode("utf-8"))
    if REGISTRY_PLAIN.exists():
        if not _warned:
            print("⚠️ 未配置 FERNET_KEY：账号登记表以明文存储（users/registry.json）。"
                  "Public 仓库请务必配置 FERNET_KEY。")
            _warned = True
        return json.loads(REGISTRY_PLAIN.read_text(encoding="utf-8"))
    return []


def save_registry(entries):
    ws.USERS_DIR.mkdir(parents=True, exist_ok=True)
    f = _fernet()
    if f:
        REGISTRY_ENC.write_bytes(f.encrypt(json.dumps(entries, ensure_ascii=False).encode("utf-8")))
        if REGISTRY_PLAIN.exists():
            REGISTRY_PLAIN.unlink()  # 加密模式下不留明文副本
    else:
        if not _warned:
            print("⚠️ 未配置 FERNET_KEY：账号登记表以明文存储（users/registry.json）。")
            globals()["_warned"] = True
        REGISTRY_PLAIN.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")


def find_account(email):
    h = hash_email(email)
    for e in load_registry():
        if e.get("dir") == h:
            return e
    return None


def create_account(email):
    """登记新账号并建目录。返回账号 dict；已存在则返回 None"""
    email = email.lower().strip()
    if find_account(email):
        return None
    h = hash_email(email)
    account = {"dir": h, "email": email, "created_at": date.today().isoformat(), "active": True}
    entries = load_registry()
    entries.append(account)
    save_registry(entries)
    base = ws.USERS_DIR / h
    (base / "config").mkdir(parents=True, exist_ok=True)
    (base / "data").mkdir(parents=True, exist_ok=True)
    return account


def active_accounts():
    """所有 active 账号（登记顺序）"""
    return [e for e in load_registry() if e.get("active", True)]


def has_users_dir():
    """多账号模式判定：users/ 目录存在（含任意内容）即为多账号模式"""
    return ws.USERS_DIR.exists() and any(ws.USERS_DIR.iterdir())
