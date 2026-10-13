#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""工作区路径管理：单用户模式（根目录 config/ + data/）与多账号模式（users/<hash>/）统一抽象。
各模块通过 use_root()/use_account() 切换当前工作区，再经 settings_path() 等取路径。
docs/（概览页）全账号共享，不在工作区内。
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parent
USERS_DIR = ROOT / "users"
DOCS_DIR = ROOT / "docs"

_current = None  # None = 单用户模式（根目录）


def use_root():
    """单用户模式：配置与数据在仓库根目录 config/ 与 data/"""
    global _current
    _current = None


def use_account(account_dir):
    """多账号模式：配置与数据在 users/<email_hash>/ 下"""
    global _current
    _current = Path(account_dir)


def is_account_mode():
    return _current is not None


def base_dir():
    return _current if _current is not None else ROOT


def config_dir():
    return base_dir() / "config"


def data_dir():
    return base_dir() / "data"


def settings_path():
    return config_dir() / "settings.json"


def profile_path():
    return config_dir() / "interest_profile.json"


def ledger_path():
    return data_dir() / "pushed.json"


def rejected_path():
    return data_dir() / "rejected.json"


def cache_path(day):
    return data_dir() / f"candidates_cache_{day}.json"


def preview_path():
    return data_dir() / "preview.html"
