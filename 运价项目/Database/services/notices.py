"""Pure announcement validation and allow-list rich-text sanitization."""
import base64
import binascii
import json
import re
from datetime import date
from html import escape
from html.parser import HTMLParser
from core.notice_contracts import (NOTICE_TITLE_MAX, NOTICE_TAG_MAX, NOTICE_TAG_COUNT_MAX,
    NOTICE_HTML_MAX, NOTICE_IMAGE_MAX, NOTICE_IMAGE_COUNT_MAX, NOTICE_SCOPE_MAX)
from core.contracts import ADMIN_ROLES

TAGS = frozenset({'p','div','br','strong','b','em','i','u','s','strike','ul','ol','li',
                  'blockquote','h2','h3','pre','span','a','img','font'})
VOID = frozenset({'br','img'})
BLOCKED = frozenset({'script','style','svg','math','iframe','object','embed','template'})
ALIGNMENTS = frozenset({'left','center','right'})
COLORS = re.compile(r'^(#[0-9a-fA-F]{3}(?:[0-9a-fA-F]{3})?|rgb\(\s*\d{1,3}\s*,\s*\d{1,3}\s*,\s*\d{1,3}\s*\))$')
FONT_SIZES = {'1':'11px','2':'13px','3':'16px','4':'18px','5':'24px','6':'30px','7':'36px'}
EXPIRY_DATE = re.compile(r'^\d{4}-\d{2}-\d{2}$')
PIN_SEARCH_LABELS = {'approved':'置顶', 'pending':'置顶待审核', 'rejected':'置顶未通过', 'none':''}


def notice_expiry(value, today):
    if value is None:
        return None
    if not isinstance(value, str) or not EXPIRY_DATE.fullmatch(value):
        raise ValueError('有效期须为截止日期 YYYY-MM-DD 或长期')
    try:
        expiry = date.fromisoformat(value)
    except ValueError as error:
        raise ValueError('有效期日期无效') from error
    if expiry < date.fromisoformat(today):
        raise ValueError('有效期不能早于今天')
    return expiry.isoformat()


def safe_image(src):
    match = re.fullmatch(r'data:image/(png|jpeg|gif|webp);base64,([A-Za-z0-9+/=\r\n]+)', src)
    if not match:
        raise ValueError('图片只允许粘贴或上传 PNG、JPEG、GIF、WebP')
    try:
        raw = base64.b64decode(match[2], validate=True)
    except (ValueError, binascii.Error) as error:
        raise ValueError('图片编码无效') from error
    if len(raw) > NOTICE_IMAGE_MAX:
        raise ValueError('每张图片不能超过 2 MB')
    valid = {'png': raw.startswith(b'\x89PNG\r\n\x1a\n'),
             'jpeg': raw.startswith(b'\xff\xd8\xff'),
             'gif': raw.startswith((b'GIF87a', b'GIF89a')),
             'webp': raw.startswith(b'RIFF') and raw[8:12] == b'WEBP'}
    if not valid[match[1]]:
        raise ValueError('图片格式与内容不匹配')
    return src


class RichText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.output, self.stack, self.blocked = [], [], []
        self.text, self.images = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in BLOCKED or self.blocked:
            self.blocked.append(tag)
            return
        if tag not in TAGS:
            return
        attrs = dict(attrs)
        styles = []
        for item in (attrs.get('style') or '').split(';'):
            key, _, val = item.partition(':')
            key, val = key.strip().lower(), val.strip()
            if key in ('color','background-color') and COLORS.fullmatch(val):
                styles.append(key + ':' + val)
            elif key == 'text-align' and val in ALIGNMENTS:
                styles.append(key + ':' + val)
            elif key == 'font-size' and val in FONT_SIZES.values():
                styles.append(key + ':' + val)
        if tag == 'font':
            color = attrs.get('color') or ''
            if COLORS.fullmatch(color):
                styles.append('color:' + color)
            if attrs.get('size') in FONT_SIZES:
                styles.append('font-size:' + FONT_SIZES[attrs['size']])
            tag = 'span'
        rendered = ' style="' + escape(';'.join(styles), quote=True) + '"' if styles else ''
        if tag == 'a':
            href = (attrs.get('href') or '').strip()
            if re.match(r'^https?://[^\s]+$', href, re.I) or re.match(r'^mailto:[^\s]+$', href, re.I):
                rendered += ' href="' + escape(href, quote=True) + '" rel="noopener noreferrer" target="_blank"'
        if tag == 'img':
            self.images += 1
            if self.images > NOTICE_IMAGE_COUNT_MAX:
                raise ValueError('每条公告最多 8 张图片')
            rendered += ' src="' + escape(safe_image(attrs.get('src') or ''), quote=True) + '" alt="公告图片"'
        self.output.append('<' + tag + rendered + '>')
        if tag not in VOID:
            self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if self.blocked:
            if tag in self.blocked:
                index = len(self.blocked) - 1 - self.blocked[::-1].index(tag)
                self.blocked = self.blocked[:index]
            return
        tag = 'span' if tag == 'font' else tag
        if tag in self.stack:
            index = len(self.stack) - 1 - self.stack[::-1].index(tag)
            while len(self.stack) > index:
                self.output.append('</' + self.stack.pop() + '>')

    def handle_data(self, data):
        if not self.blocked:
            self.output.append(escape(data))
            self.text.append(data)

    def finish(self):
        self.close()
        while self.stack:
            self.output.append('</' + self.stack.pop() + '>')
        if not ''.join(self.text).replace('\xa0', ' ').strip() and not self.images:
            raise ValueError('公告正文不能为空')
        return ''.join(self.output)


class VisibleText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)


def notice_matches(row, query):
    word = str(query or '').strip().casefold()
    if not word:
        return True
    parser = VisibleText()
    parser.feed(str(row.get('body_html') or ''))
    tags = row.get('tags') or []
    visible = [row.get('title'), ' '.join(parser.parts), row.get('author_name'),
        row.get('group_name') or '全体', ' '.join(tags), row.get('created_at'),
        row.get('updated_at'), row.get('expires_on'), PIN_SEARCH_LABELS.get(row.get('pin_state'), ''),
        '已更新' if int(row.get('revision') or 1) > 1 else '']
    groups, routes = notice_targets(row)
    visible.extend(groups + routes)
    return word in ' '.join(str(value or '') for value in visible).casefold()


def notice_scope_values(value):
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError('公告范围必须为文本列表')
    values = list(dict.fromkeys(item.strip() for item in value if item.strip()))
    if len(values) > NOTICE_SCOPE_MAX:
        raise ValueError(f'每种公告范围最多选择 {NOTICE_SCOPE_MAX} 项')
    return values


def notice_targets(row):
    groups = json.loads(row.get('target_groups_json') or '[]')
    routes = json.loads(row.get('target_routes_json') or '[]')
    return (groups or ([row['group_name']] if row.get('group_name') else []), routes)


def notice_scope_matches(row, groups, routes, include_global=True):
    targets, lanes = notice_targets(row)
    if not groups and not routes:
        return True
    if not targets and not lanes:
        return include_global
    return bool(set(groups).intersection(targets) or set(routes).intersection(lanes))


def notice_form(body, groups, routes=()):
    title = str(body.get('title') or '').strip()
    if not title or len(title) > NOTICE_TITLE_MAX:
        raise ValueError('标题必填，最多 160 字')
    group = str(body.get('group_name') or '').strip()
    if group and group not in groups:
        raise ValueError('请选择启用的群分类')
    target_groups = notice_scope_values(body.get('target_groups', [group] if group else []))
    target_routes = notice_scope_values(body.get('target_routes', []))
    if set(target_groups) - set(groups) or set(target_routes) - set(routes):
        raise ValueError('请选择启用的群聊和航线')
    group = target_groups[0] if len(target_groups) == 1 and not target_routes else ''
    tags = body.get('tags') or []
    if not isinstance(tags, list) or any(not isinstance(tag, str) for tag in tags):
        raise ValueError('标签必须为文本列表')
    tags = list(dict.fromkeys(tag.strip() for tag in tags if tag.strip()))
    if len(tags) > NOTICE_TAG_COUNT_MAX or any(len(tag) > NOTICE_TAG_MAX for tag in tags):
        raise ValueError('最多 8 个标签，每个最多 24 字')
    html = str(body.get('body_html') or '')
    if len(html.encode('utf-8')) > NOTICE_HTML_MAX:
        raise ValueError('公告正文过大')
    parser = RichText()
    parser.feed(html)
    if 'pin' in body and not isinstance(body['pin'], bool):
        raise ValueError('置顶参数必须为布尔值')
    return {'title':title, 'body_html':parser.finish(), 'group_name':group, 'tags':tags,
            'target_groups':target_groups, 'target_routes':target_routes}


def pin_transition(state, action, role, owner):
    if action in ('approve','reject'):
        if role not in ADMIN_ROLES:
            raise PermissionError('只有管理员可以审核置顶')
        if state != 'pending':
            raise ValueError('该公告没有待审核的置顶申请')
        return 'approved' if action == 'approve' else 'rejected'
    if not owner and role not in ADMIN_ROLES:
        raise PermissionError('只能修改本人公告的置顶状态')
    if action == 'request':
        return 'pending' if role == 'sales' else 'approved'
    if action == 'unpin':
        return 'none'
    raise ValueError('未知置顶操作')
