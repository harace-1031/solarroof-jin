"""Server-only parcel address collection. No visitor identifiers are stored."""
import os
import json
import io
import zipfile
import hashlib
import hmac
import time
from threading import Lock
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from xml.sax.saxutils import escape
from datetime import datetime, timezone, timedelta

_lock = Lock()
_failures = []

class StorageError(Exception):
    pass

def storage_error(error):
    if isinstance(error, StorageError):
        return str(error)
    return '저장소 응답 처리 실패입니다. 최신 app.py와 search_records.py를 함께 업로드했는지 확인하세요.'

def configured():
    return bool(os.getenv('SUPABASE_URL') and os.getenv('SUPABASE_SECRET_KEY'))

def request_db(method, query, data=None):
    base = os.getenv('SUPABASE_URL', '').rstrip('/')
    key = os.getenv('SUPABASE_SECRET_KEY', '')
    if not base:
        raise StorageError('Render Environment에 SUPABASE_URL이 없습니다.')
    if not key:
        raise StorageError('Render Environment에 SUPABASE_SECRET_KEY가 없습니다.')
    if base != base.strip() or key != key.strip():
        raise StorageError('연결 값 앞뒤에 공백 또는 줄바꿈이 있습니다. Render의 두 값을 다시 입력하세요.')
    parsed = urlsplit(base)
    if not configured() or parsed.scheme != 'https' or not (parsed.hostname or '').endswith('.supabase.co') or parsed.path or parsed.query or parsed.fragment or parsed.username:
        raise StorageError('SUPABASE_URL 형식이 잘못됐습니다. https://프로젝트ID.supabase.co 형태의 프로젝트 주소만 입력하세요. /dashboard 또는 /rest/v1은 붙이지 않습니다.')
    if key.startswith('sb_publishable_'):
        raise StorageError('Publishable 키가 입력됐습니다. 같은 프로젝트의 Secret key를 SUPABASE_SECRET_KEY에 입력하세요.')
    headers = {'apikey': key, 'Content-Type': 'application/json', 'Prefer': 'return=minimal'}
    if not key.startswith('sb_secret_'):
        headers['Authorization'] = 'Bearer ' + key
    body = json.dumps(data, ensure_ascii=False).encode() if data is not None else None
    req = Request(base + '/rest/v1/roof_building_search_events?' + urlencode(query), data=body, headers=headers, method=method)
    try:
        with urlopen(req, timeout=12) as response:
            raw = response.read()
    except HTTPError as error:
        try:
            code = json.loads(error.read(8192)).get('code','')
        except Exception:
            code = ''
        if code in {'PGRST205','42P01'}:
            message = 'roof_building_search_events 표를 찾지 못했습니다. Render에 연결한 것과 같은 Supabase 프로젝트에서 검색기록표_생성.sql을 실행하세요.'
        elif code in {'PGRST204','42703'}:
            message = '표의 열이 일치하지 않습니다. roof_building_search_events에 id, address, searched_at 열이 모두 있어야 합니다.'
        elif error.code == 401:
            message = 'Supabase 인증 실패(401)입니다. URL과 Secret key가 같은 프로젝트의 값인지, 키 전체가 복사됐는지 확인하세요. anon/Publishable 키는 사용하지 않습니다.'
        elif error.code == 403 or code == '42501':
            message = 'Supabase 접근 권한이 없습니다. Secret key 또는 service_role 키를 사용하고 검색기록표_생성.sql의 권한 설정까지 실행하세요.'
        elif error.code == 404:
            message = 'Supabase 주소를 찾지 못했습니다(404). 프로젝트 URL과 프로젝트의 실행 상태를 확인하세요.'
        else:
            message = f'Supabase가 HTTP {error.code}로 응답했습니다. 프로젝트가 실행 중인지 확인하세요.'
        raise StorageError(message) from None
    except (URLError, TimeoutError):
        raise StorageError('Supabase 서버에 연결하지 못했습니다. 프로젝트 URL 철자와 Supabase 프로젝트의 일시중지 여부를 확인하세요.') from None
    return json.loads(raw) if raw else None

def record(address):
    if not configured():
        return 'disabled'
    if not isinstance(address, str) or not address.strip() or len(address) > 300:
        return 'error'
    try:
        request_db('POST', {}, [{'address': address.strip()}])
        return 'saved'
    except Exception:
        # Never log address, credentials, or upstream response bodies.
        return 'error'

def authenticate(password):
    expected = os.getenv('ADMIN_PASSWORD', '')
    if not expected:
        return 'disabled'
    with _lock:
        now = time.monotonic()
        _failures[:] = [t for t in _failures if now - t < 60]
        if len(_failures) >= 10:
            return 'limited'
        if not hmac.compare_digest(hashlib.sha256(password.encode()).digest(), hashlib.sha256(expected.encode()).digest()):
            _failures.append(now)
            return 'denied'
    return 'ok'

def addresses():
    # Follow the actual page size, including projects configured below 1,000 rows.
    result = []
    cursor = 0
    latest = request_db('GET', {'select':'id','order':'id.desc','limit':'1'})
    if not latest:return result
    upper = int(latest[0]['id'])
    while True:
        query = {'select': 'id,address,searched_at', 'order': 'id.asc', 'limit': '1000', 'id':'gt.'+str(cursor), 'and':f'(id.lte.{upper})'}
        rows = request_db('GET', query)
        if not isinstance(rows, list):
            raise ValueError('저장 목록 조회 실패')
        if not rows:
            return result
        page = [(r['address'], datetime.fromisoformat(r['searched_at'].replace('Z','+00:00')).astimezone(timezone(timedelta(hours=9))).strftime('%Y-%m-%d %H:%M:%S')) for r in rows]
        if int(rows[-1]['id']) <= cursor:
            raise ValueError('저장 목록 페이지 오류')
        result.extend(page)
        cursor = int(rows[-1]['id'])
        if cursor >= upper:return result
        if len(result) > 100000:
            raise ValueError('내려받기 범위를 초과했습니다.')

def workbook(items):
    def cell(value, ref):
        # Explicit text cells prevent Excel formula interpretation.
        clean = ''.join(c for c in str(value) if ord(c) >= 32 or c in '\t\n\r')
        return f'<c r="{ref}" t="inlineStr"><is><t xml:space="preserve">{escape(clean)}</t></is></c>'
    rows = [('검색 건물주소','검색일시 (한국시간)'), *items]
    sheet = '<?xml version="1.0" encoding="UTF-8"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><cols><col min="1" max="1" width="65" customWidth="1"/><col min="2" max="2" width="25" customWidth="1"/></cols><sheetData>' + ''.join(f'<row r="{i}">'+cell(a,f'A{i}')+cell(t,f'B{i}')+'</row>' for i,(a,t) in enumerate(rows,1)) + '</sheetData></worksheet>'
    files = {
        '[Content_Types].xml': '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>',
        '_rels/.rels': '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>',
        'xl/workbook.xml': '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="검색 건물" sheetId="1" r:id="rId1"/></sheets></workbook>',
        'xl/_rels/workbook.xml.rels': '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>',
        'xl/worksheets/sheet1.xml': sheet,
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content.encode())
    return buffer.getvalue()
