"""SOLARROOF_JIN: loopback-only rooftop planning workbench."""
import sys, json, math, secrets, urllib.parse, urllib.request, io, os
from pathlib import Path
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
ROOT=Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT))
import land_source as land
from shapely.geometry import Polygon, box
from shapely.ops import transform, unary_union
from pyproj import Transformer
TO_LOCAL=Transformer.from_crs(3857,5179,always_xy=True).transform
TO_MAP=Transformer.from_crs(5179,3857,always_xy=True).transform
SESSION=secrets.token_urlsafe(32)
SETTINGS=ROOT/'settings.local.json'
TILES={}
ADDRESSES={}

def number(d,k,default,lo,hi):
    v=float(d.get(k,default))
    if not math.isfinite(v) or not lo<=v<=hi:raise ValueError(f'{k}: {lo}~{hi} 범위로 입력하세요.')
    return v

def polygon(points):
    if not isinstance(points,list) or not 3<=len(points)<=300:raise ValueError('윤곽은 3~300개의 점으로 지정하세요.')
    for p in points:
        if len(p)!=2 or not all(isinstance(x,(float,int)) and math.isfinite(x) for x in p):raise ValueError('윤곽 좌표가 올바르지 않습니다.')
        if not 13800000<p[0]<14700000 or not 3800000<p[1]<4800000:raise ValueError('대한민국 내 위치를 지정하세요.')
    g=transform(TO_LOCAL,Polygon(points))
    if not g.is_valid or g.area<1 or g.area>100000:raise ValueError('윤곽이 교차하거나 면적이 너무 작거나 큽니다.')
    return g

def plan(data):
    cfg=data.get('config',{})
    length=number(cfg,'length',2382,100,6000)/1000
    width=number(cfg,'width',1134,100,6000)/1000
    wp=number(cfg,'wp',650,1,3000)
    roofs=data.get('roofs',[]); exclusions=data.get('exclusions',[])
    if not 1<=len(roofs)<=30 or len(exclusions)>100:raise ValueError('지붕면 1~30개, 제외 영역 최대 100개를 지정하세요.')
    holes=[polygon(p) for p in exclusions]
    occupied=[]; output=[]; modules=[];total=0
    for idx,r in enumerate(roofs):
        roof=polygon(r['points'])
        if any(roof.intersection(other).area>.05 for other in occupied):raise ValueError('지붕면이 겹칩니다. 용마루를 기준으로 나눠 그려주세요.')
        occupied.append(roof)
        slope=number(r,'slope',10,0,60)
        bearing=number(r,'bearing',180,0,360)
        beta=math.radians(slope); a=math.radians(bearing)
        # Local x follows the eaves, y follows the uphill roof surface.
        u=(math.cos(a),-math.sin(a));v=(-math.sin(a),-math.cos(a))
        ox,oy=roof.centroid.coords[0]; c=math.cos(beta)
        def into(x,y,z=None):return ((x-ox)*u[0]+(y-oy)*u[1],((x-ox)*v[0]+(y-oy)*v[1])/c)
        def out(x,y,z=None):return (ox+x*u[0]+y*c*v[0],oy+x*u[1]+y*c*v[1])
        surface=transform(into,roof)
        usable=surface.buffer(-1,join_style=2)
        if holes:usable=usable.difference(unary_union([transform(into,h) for h in holes]))
        # Flush mount; flat roofs use selected south-facing mounting tilt and 25-degree sun spacing.
        flat=r.get('type')=='flat'
        if flat and slope!=0:raise ValueError('평지붕의 지붕 경사는 0도로 설정하세요.')
        tilt=number(cfg,'tilt',10,0,30) if flat else slope
        h=length*math.cos(math.radians(tilt)) if flat else length
        gap=.01
        if flat:gap=max(.01,length*math.sin(math.radians(tilt))/math.tan(math.radians(25)))
        coords=[]
        if not usable.is_empty:
            xmin,ymin,xmax,ymax=usable.bounds
            nx=max(0,int((xmax-xmin+.01)/(width+.01)))
            ny=max(0,int((ymax-ymin+gap)/(h+gap)))
            if nx*ny>100000:raise ValueError('지붕 범위가 너무 큽니다.')
            # Search several grid offsets to reduce avoidable empty strips.
            best=[]
            for fx,fy in [(0,0),(.5,0),(0,.5),(.5,.5)]:
                trial=[]
                for iy in range(ny+1):
                    y=ymin+(iy+fy)*(h+gap)
                    for ix in range(nx+1):
                        x=xmin+(ix+fx)*(width+.01)
                        cell=box(x,y,x+width,y+h)
                        if usable.covers(cell):trial.append(cell)
                if len(trial)>len(best):best=trial
            for cell in best:
                local=transform(out,cell)
                coords.append(list(transform(TO_MAP,local).exterior.coords)[:-1])
                modules.append(list(local.exterior.coords)[:-1])
        if len(modules)>20000:raise ValueError('최대 20,000장까지 배치할 수 있습니다.')
        total+=surface.area
        output.append({'name':str(r.get('name',f'지붕 {idx+1}'))[:80],'modules':coords,'count':len(coords),'surfaceArea':surface.area,'planArea':roof.area,'slope':slope,'bearing':bearing,'gap':gap,'tilt':tilt})
    capacity=len(modules)*wp/1000
    finance=economics(capacity,cfg)
    return {'roofs':output,'count':len(modules),'capacity':capacity,'surfaceArea':total,'finance':finance,'cadModules':modules,'cadRoofs':[list(p.exterior.coords) for p in occupied],'cadExclusions':[list(p.exterior.coords) for p in holes],'setback':1}

def economics(kw,c):
    hours=number(c,'hours',3.6,.1,8); price=number(c,'price',170,0,1000)
    epc=number(c,'epc',90,1,1000); extra=number(c,'extra',0,0,1000000)
    rent=number(c,'rent',0,0,100000); om=number(c,'om',1,0,20)/100
    degrade=number(c,'degrade',.5,0,5)/100; discount=number(c,'discount',5,0,30)/100
    cost=(kw*epc+extra)*10000; cumulative=-cost; rows=[];payback=None;npv=-cost
    for year in range(1,26):
        energy=kw*hours*365*(1-degrade)**(year-1)
        revenue=energy*price;expense=cost*om+rent*10000;net=revenue-expense
        before=cumulative;cumulative+=net;npv+=net/(1+discount)**year
        if payback is None and cumulative>=0 and net>0:payback=year-1+(-before/net)
        rows.append({'year':year,'energy':energy,'revenue':revenue,'expense':expense,'net':net,'cumulative':cumulative})
    return {'cost':cost,'npv':npv,'payback':payback,'rows':rows}

def ledger(pnu):
    land.pnu_valid(pnu)
    config={'buildingKey':os.environ.get('BUILDING_API_KEY','')}
    key=config.get('buildingKey','').strip()
    if not key:return {'status':'key_required','items':[],'message':'건축물대장 자동 조회를 사용하려면 설정에서 공공데이터포털 인증키를 등록하세요.'}
    args={'serviceKey':urllib.parse.unquote(key),'sigunguCd':pnu[:5],'bjdongCd':pnu[5:10],'platGbCd':'1' if pnu[10]=='2' else '0','bun':pnu[11:15],'ji':pnu[15:],'numOfRows':100,'pageNo':1,'_type':'json'}
    url='https://apis.data.go.kr/1613000/BldRgstHubService/getBrTitleInfo?'+urllib.parse.urlencode(args)
    try:
        raw=json.loads(land.fetch(url)); response=raw['response']
        if str(response['header']['resultCode']) not in ('00','0'):raise ValueError('건축물대장 API 인증 또는 활용 승인을 확인하세요.')
        body=response.get('body',{}); items=(body.get('items') or {}).get('item',[]) if isinstance(body.get('items'),dict) else []
        if isinstance(items,dict):items=[items]
        fields=['bldNm','dongNm','mainPurpsCdNm','strctCdNm','etcStrct','roofCdNm','etcRoof','archArea','totArea','grndFlrCnt','heit','useAprDay']
        return {'status':'ok','items':[{k:i.get(k,'') for k in fields} for i in items], 'message':'건축HUB 대장 정보 조회 결과 · 공식 증명서 발급이 아닙니다.'}
    except ValueError:raise
    except Exception:raise ValueError('건축물대장 조회에 실패했습니다. 인증키·활용 승인 상태를 확인하세요.') from None

class Handler(SimpleHTTPRequestHandler):
    def __init__(self,*a,**kw):super().__init__(*a,directory=str(ROOT/'static'),**kw)
    def log_message(self,*a):pass
    def send(self,body,status=200,kind='application/json; charset=utf-8',filename=None):
        if not isinstance(body,bytes):body=json.dumps(body,ensure_ascii=False).encode()
        self.send_response(status);self.send_header('Content-Type',kind);self.send_header('Content-Length',str(len(body)));self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff')
        if filename:self.send_header('Content-Disposition',f'attachment; filename="{filename}"')
        self.end_headers();self.wfile.write(body)
    def safe(self):
        return self.headers.get('Host') in ('127.0.0.1:8790','localhost:8790')
    def do_GET(self):
        if not self.safe():return self.send({'error':'로컬 주소로 접속하세요.'},403)
        u=urllib.parse.urlparse(self.path);q=urllib.parse.parse_qs(u.query)
        try:
            if u.path=='/api/status':return self.send({'app':'SOLARROOF_JIN','pid':os.getpid(),'token':SESSION,'keyConfigured':SETTINGS.exists() and bool(json.loads(SETTINGS.read_text('utf-8')).get('buildingKey'))})
            if u.path=='/api/search':
                found=land.search_land(q.get('q',[''])[0])
                for item in found['results']:ADDRESSES[item['pnu']]=item['address']
                return self.send(found)
            if u.path=='/api/center':
                p=land.pnu_valid(q.get('pnu',[''])[0]);x,y=land.parcel_center(p,land.map_version(p));return self.send({'center':list(TO_MAP(x,y))})
            if u.path=='/api/ledger':return self.send(ledger(q.get('pnu',[''])[0]))
            if u.path=='/api/grid':
                p=land.pnu_valid(q.get('pnu',[''])[0])
                address=ADDRESSES.get(p) or land.land_detail(p)['address']
                return self.send(land.grid_detail(address,p))
            if u.path=='/api/tile':
                z=int(q['z'][0]);x=int(q['x'][0]);y=int(q['y'][0])
                if not 8<=z<=20 or not 0<=x<2**z or not 0<=y<2**z:raise ValueError('타일 범위 오류')
                k=(z,x,y)
                if k not in TILES:
                    raw=land.fetch(f'https://services.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}')
                    if not raw.startswith((b'\xff\xd8',b'\x89PNG')):raise ValueError('영상 없음')
                    if len(TILES)>600:TILES.clear()
                    TILES[k]=raw
                return self.send(TILES[k],kind='image/jpeg')
            if u.path.startswith('/api/'):return self.send({'error':'없는 경로입니다.'},404)
            return super().do_GET()
        except urllib.error.URLError:return self.send({'error':'외부 조회 서버에 연결하지 못했습니다. SOLARROOF_JIN 재시작 파일로 다시 실행한 뒤 조회하세요. 계속 실패하면 인터넷 연결 또는 공식 사이트 상태를 확인하세요.'},502)
        except ValueError as e:return self.send({'error':str(e)},422)
        except Exception:return self.send({'error':'조회 서비스 연결에 실패했습니다. 잠시 후 다시 시도하세요.'},502)
    def do_POST(self):
        if not self.safe() or self.headers.get('X-Local-Token')!=SESSION or self.headers.get('Origin') not in ('http://127.0.0.1:8790','http://localhost:8790'):return self.send({'error':'로컬 페이지에서 다시 시도하세요.'},403)
        try:
            size=int(self.headers.get('Content-Length','0'))
            if not 0<size<=2000000:raise ValueError('요청 크기를 확인하세요.')
            data=json.loads(self.rfile.read(size))
            if self.path=='/api/settings':
                key=str(data.get('buildingKey','')).strip()
                if len(key)>500:raise ValueError('키 길이를 확인하세요.')
                SETTINGS.write_text(json.dumps({'buildingKey':key}),encoding='utf-8');return self.send({'ok':True})
            if self.path=='/api/plan':return self.send(plan(data))
            if self.path=='/api/dxf':
                import ezdxf
                result=plan(data);doc=ezdxf.new('R2000');doc.units=6;m=doc.modelspace()
                for name,color,polys in [('ROOF',3,result['cadRoofs']),('EXCLUSION',1,result['cadExclusions']),('PV_MODULE',5,result['cadModules'])]:
                    doc.layers.new(name,dxfattribs={'color':color})
                    for p in polys:m.add_lwpolyline(p,close=True,dxfattribs={'layer':name})
                from ezdxf import zoom
                zoom.extents(m);stream=io.StringIO();doc.write(stream)
                return self.send(stream.getvalue().encode('ascii','backslashreplace'),kind='application/dxf',filename='SOLARROOF_JIN.dxf')
            return self.send({'error':'없는 경로입니다.'},404)
        except (ValueError,KeyError,TypeError) as e:return self.send({'error':str(e)},422)
        except Exception:return self.send({'error':'계산 처리에 실패했습니다. 윤곽과 입력값을 확인하세요.'},500)

if __name__=='__main__':
    print('SOLARROOF_JIN http://127.0.0.1:8790/',flush=True)
    ThreadingHTTPServer(('127.0.0.1',8790),Handler).serve_forever()
