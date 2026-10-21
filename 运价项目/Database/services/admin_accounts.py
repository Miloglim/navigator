"""Pure validation and projections for personnel administration."""
import hashlib
import json
import re
from datetime import date
from core.admin_contracts import (PROFILE_FIELDS, EXPERIENCE_FIELDS, ADMIN_TEXT_LIMIT,
    ADMIN_NOTE_LIMIT, MIN_CAREER_YEAR, SECURITY_ACTIONS, ADMIN_SERVER_FIELDS)
from core.contracts import ACCOUNT_NAME_MAX_LENGTH
from services.account_forms import new_password


def profile_fields(section, fields, today):
    allowed = PROFILE_FIELDS if section == 'profile' else EXPERIENCE_FIELDS if section == 'experience' else frozenset()
    if not isinstance(fields, dict) or not fields or not allowed or set(fields) - allowed:
        raise ValueError('只允许修改当前资料区的字段')
    cleaned = {}
    for key, value in fields.items():
        if value is not None and not isinstance(value, (str, int)):
            raise ValueError('资料字段必须是文字')
        text = str(value if value is not None else '').strip()
        maximum = ADMIN_NOTE_LIMIT if key in ('notes','skills') else ADMIN_TEXT_LIMIT
        if key == 'display_name':maximum = ACCOUNT_NAME_MAX_LENGTH
        if len(text) > maximum or any(ord(c) < 32 and c not in '\n\t' for c in text):
            raise ValueError('资料过长或含非法控制字符：'+key)
        if key == 'display_name' and not text:
            raise ValueError('用户名不能为空')
        if key == 'joined_on' and text:
            date.fromisoformat(text)
        if key == 'career_start_year' and text:
            if not re.fullmatch(r'\d{4}', text) or not MIN_CAREER_YEAR <= int(text) <= today.year:
                raise ValueError('从业起始年份无效')
        if key == 'work_email' and text and not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', text):
            raise ValueError('工作邮箱格式无效')
        if key == 'employment' and text not in ('','在职','离职'):
            raise ValueError('在职状态无效')
        cleaned[key] = text
    return cleaned


def signature(account, profile, experience, required):
    facts = {k: account.get(k) for k in ('user_id','name','publisher_id','publisher_name','display_name','role','active','password_changed_at','security_revision')}
    return hashlib.sha256(json.dumps([facts,profile,experience,required],ensure_ascii=False,sort_keys=True,default=str).encode('utf-8')).hexdigest()


def login_state(account):
    if not account.get('has_password'):
        return 'unopened'
    return 'enabled' if account.get('active') else 'disabled'


def security_request(body):
    action = body.get('action')
    allowed = {'user_id','action','expected_signature','role','new_name','new_password','confirm_password'} | ADMIN_SERVER_FIELDS
    if set(body) - allowed or action not in SECURITY_ACTIONS:
        raise ValueError('不支持的账号安全操作')
    if action in ('activate','reset_password'):
        new_password(body.get('new_password'),body.get('confirm_password'))
    if action == 'role' and body.get('role') not in ('admin','operator','sales'):
        raise ValueError('角色无效')
    if action == 'rename_login':
        from services.account_forms import account_name
        account_name(body.get('new_name'))
    return action
