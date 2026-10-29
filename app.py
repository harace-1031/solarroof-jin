"""Render WSGI entry point. Local settings never writable through the cloud app."""
import io,json,mimetypes,os,secrets,threading,urllib.parse
from pathlib import Path
import server as core
import search_records
ROOT=Path(__file__).resolve().parent
TOKEN=secrets.token_urlsafe(32)
CALC=threading.BoundedSemaphore(1)
GRID=threading.BoundedSemaphore(1)
LOOKUP=threading.BoundedSemaphore(3)

def application(env,start_response):
    def reply(data,status=200,kind='application/json; charset=utf-8',extra=()):
        if not isinstance(data,bytes):data=json.dumps(data,ensure_ascii=False).encode()
        reason={200:'OK',400:'Bad Request',403:'Forbidden',404:'Not Found',405:'Method Not Allowed',413:'Content Too Large',422:'Unprocessable Entity',429:'Too Many Requests',502:'Bad Gateway',503:'Service Unavailable'}.get(status,'Error')
        start_response(f'{status} {reason}',[('Content-Type',kind),('Content-Length',str(len(data))),('Cache-Control','no-store'),('X-Content-Type-Options','nosniff'),('Referrer-Policy','same-origin'),*extra])
        return [b'' if env.get('REQUEST_METHOD')=='HEAD' else data]
    path=env.get('PATH_INFO','/');method=env.get('REQUEST_METHOD','GET');host=env.get('HTTP_HOST','').lower()
    allowed={'solarroof.jinenergy.co.kr','127.0.0.1','localhost'}
    renderhost=os.environ.get('RENDER_EXTERNAL_HOSTNAME','').lower()
    if renderhost:allowed.add(renderhost)
    if host.split(':')[0] not in allowed:return reply({'error':'허용되지 않은 호스트입니다.'},403)
    if method not in ('GET','HEAD','POST'):return reply({'error':'허용되지 않은 요청입니다.'},405)
    q=urllib.parse.parse_qs(env.get('QUERY_STRING',''));slot=None
    try:
        if path in ('/admin','/admin/') and method in ('GET','HEAD'):
            return reply((ROOT/'static/admin.html').read_bytes(),kind='text/html; charset=utf-8')
        if path=='/admin/export':
            if method!='POST':return reply({'error':'관리자 화면에서 비밀번호를 입력하세요.'},405)
            origin=env.get('HTTP_ORIGIN')
            if origin and origin not in ('https://'+host,'http://'+host if host.split(':')[0] in ('localhost','127.0.0.1') else 'https://'+host):return reply({'error':'허용되지 않은 요청입니다.'},403)
            size=int(env.get('CONTENT_LENGTH') or '0')
            if not 0<size<=4096:return reply({'error':'잘못된 요청입니다.'},400)
            form=urllib.parse.parse_qs(env['wsgi.input'].read(size).decode())
            auth=search_records.authenticate(form.get('password',[''])[0])
            if auth!='ok':
                code=429 if auth=='limited' else 503 if auth=='disabled' else 403
                msg='잠시 후 다시 시도하세요.' if auth=='limited' else 'Render에 ADMIN_PASSWORD를 설정하세요.' if auth=='disabled' else '관리자 비밀번호가 일치하지 않습니다.'
                return reply(msg.encode(),code,'text/plain; charset=utf-8')
            try:
                body=search_records.workbook(search_records.addresses())
                return reply(body,kind='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',extra=[('Content-Disposition','attachment; filename="SOLARROOF_JIN_buildings.xlsx"')])
            except Exception as error:return reply(('저장소 연결 실패: '+search_records.storage_error(error)).encode(),502,'text/plain; charset=utf-8')
        if path=='/api/health':return reply({'status':'ok','app':'SOLARROOF_JIN','version':'20261029-admin-1'})
        if path=='/api/settings':return reply({'error':'인증키는 Render Environment의 BUILDING_API_KEY에서 설정하세요.'},403)
        if method=='POST':
            origin=urllib.parse.urlparse(env.get('HTTP_ORIGIN',''))
            if origin.netloc.lower()!=host or origin.scheme not in ('http','https') or env.get('HTTP_X_LOCAL_TOKEN')!=TOKEN:return reply({'error':'페이지를 새로고침한 뒤 다시 시도하세요.'},403)
            if path not in ('/api/plan','/api/dxf'):return reply({'error':'없는 경로입니다.'},404)
            length=int(env.get('CONTENT_LENGTH') or '0')
            if not 0<length<=2000000:return reply({'error':'요청 크기를 확인하세요.'},413)
            slot=CALC
            if not slot.acquire(False):slot=None;return reply({'error':'다른 배치를 계산 중입니다. 잠시 후 다시 시도하세요.'},503)
            data=json.loads(env['wsgi.input'].read(length));result=core.plan(data)
            if path=='/api/plan':return reply(result)
            import ezdxf
            from ezdxf import zoom
            doc=ezdxf.new('R2000');doc.units=6;m=doc.modelspace()
            for name,color,polys in [('ROOF',3,result['cadRoofs']),('EXCLUSION',1,result['cadExclusions']),('PV_MODULE',5,result['cadModules'])]:
                doc.layers.new(name,dxfattribs={'color':color})
                for p in polys:m.add_lwpolyline(p,close=True,dxfattribs={'layer':name})
            zoom.extents(m);out=io.StringIO();doc.write(out)
            return reply(out.getvalue().encode('ascii','backslashreplace'),kind='application/dxf',extra=[('Content-Disposition','attachment; filename="SOLARROOF_JIN.dxf"')])
        if path=='/api/status':return reply({'app':'SOLARROOF_JIN','token':TOKEN,'keyConfigured':bool(os.environ.get('BUILDING_API_KEY'))})
        if path.startswith('/api/'):
            if method=='HEAD':return reply({'error':'GET 요청을 사용하세요.'},405)
            if path not in ('/api/search','/api/center','/api/ledger','/api/grid'):return reply({'error':'없는 경로입니다.'},404)
            slot=GRID if path=='/api/grid' else LOOKUP
            if not slot.acquire(False):slot=None;return reply({'error':'조회 요청이 진행 중입니다. 잠시 후 다시 시도하세요.'},503)
            if path=='/api/search':
                found=core.land.search_land(q.get('q',[''])[0])
                if len(core.ADDRESSES)>3000:core.ADDRESSES.clear()
                for item in found['results']:core.ADDRESSES[item['pnu']]=item['address']
                return reply(found)
            p=core.land.pnu_valid(q.get('pnu',[''])[0])
            if path=='/api/center':
                x,y=core.land.parcel_center(p,core.land.map_version(p))
                collection='error'
                try:
                    address=core.ADDRESSES.get(p) or core.land.land_detail(p)['address']
                    collection=search_records.record(address)
                except Exception:pass
                return reply({'center':list(core.TO_MAP(x,y)),'collectionStatus':collection})
            if path=='/api/ledger':return reply(core.ledger(p))
            address=core.ADDRESSES.get(p) or core.land.land_detail(p)['address']
            return reply(core.land.grid_detail(address,p))
        target=(ROOT/'static'/('index.html' if path=='/' else path.lstrip('/'))).resolve()
        if not target.is_relative_to(ROOT/'static') or not target.is_file():return reply({'error':'없는 경로입니다.'},404)
        return reply(target.read_bytes(),kind=mimetypes.guess_type(str(target))[0] or 'application/octet-stream')
    except (ValueError,TypeError,KeyError):return reply({'error':'입력값 또는 조회 결과를 확인하세요. 윤곽 교차·면적·경사각 범위를 확인해 주세요.'},422)
    except Exception:return reply({'error':'외부 서비스 연결 또는 처리에 실패했습니다. 잠시 후 다시 조회하세요.'},502)
    finally:
        if slot is not None:slot.release()
