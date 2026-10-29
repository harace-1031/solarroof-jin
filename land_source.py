"""SOLAR_JIN local read-only land / distribution-grid lookup service.
Uses the public responses used by eum.go.kr and online.kepco.co.kr.
No credentials, third-party proxy, or private datasets are used.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
from html import unescape
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import http.cookiejar
import hashlib
import json
import math
import threading
import re
import secrets
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))
if __name__=='__main__':sys.modules.setdefault('server',sys.modules[__name__])
EUM = 'https://www.eum.go.kr'
KEPCO = 'https://online.kepco.co.kr'
GRID_PAGE = KEPCO + '/EWM092D00'
HEADERS = {'User-Agent': 'Mozilla/5.0', 'Accept': '*/*'}
KST = timezone(timedelta(hours=9))

def now():
    return datetime.now(KST).isoformat(timespec='seconds')

def opener():
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

def fetch(url, data=None, headers=None, client=None):
    req = urllib.request.Request(url, data=data, headers={**HEADERS, **(headers or {})})
    with (client or opener()).open(req, timeout=25) as res:
        body = res.read(6_000_001)
        if len(body) > 6_000_000:
            raise ValueError('조회 응답이 너무 큽니다.')
        return body

def clean(text):
    return re.sub(r'\s+', ' ', unescape(re.sub(r'<[^>]+>', ' ', text))).strip()

def number(text):
    m = re.search(r'-?\d[\d,]*(?:\.\d+)?', str(text))
    return float(m.group().replace(',', '')) if m else None

def pnu_valid(pnu):
    if not re.fullmatch(r'\d{19}', pnu):
        raise ValueError('올바른 필지를 선택하세요.')
    return pnu

def search_land(query):
    query = query.strip()
    if not 3 <= len(query) <= 140 or not re.fullmatch(r'[가-힣\d\s\-().]+', query):
        raise ValueError('읍·면·동(리)과 지번을 입력하세요. 예: 장현동 569')
    if not re.search(r'\d', query):
        raise ValueError('지번 숫자까지 입력하세요.')
    client = opener()
    fetch(EUM + '/web/am/amMain.jsp', client=client)
    raw = fetch(EUM + '/web/am/mp/mpSearchAddrAjaxXml.jsp',
                urllib.parse.urlencode({'sId':'selectAdAddrList', 'keyword':query.replace('번지','')}).encode(),
                {'Referer':EUM+'/web/am/amMain.jsp', 'X-Requested-With':'XMLHttpRequest',
                 'Content-Type':'application/x-www-form-urlencoded; charset=UTF-8'}, client)
    payload = json.loads(raw.decode('euc-kr'))
    results = []
    # Only parcel results: road/building results may identify several underlying parcels.
    for name in ('jibunBonBuList', 'jibunList'):
        xml = payload.get(name)
        if not xml:
            continue
        root = ET.fromstring(xml)
        for node in root.findall('.//node'):
            pnu = node.findtext('pnu', '')
            if not re.fullmatch(r'\d{19}', pnu):
                continue
            base = node.findtext('fullStr','')
            main = node.findtext('bonbun', str(int(pnu[11:15])))
            sub = node.findtext('bubun', str(int(pnu[15:19])))
            lot = ('산 ' if pnu[10]=='2' else '') + str(int(main or 0))
            if int(sub or 0):
                lot += '-' + str(int(sub))
            address = base.strip() + ' ' + lot
            if not any(r['pnu']==pnu for r in results):
                results.append({'address':address, 'pnu':pnu})
    lot_match=re.search(r'(산\s*)?(\d+)(?:\s*-\s*(\d+))?\s*$',query.replace('번지',''))
    if lot_match:
        san='2' if lot_match.group(1) else '1'
        main=int(lot_match.group(2));sub=int(lot_match.group(3) or 0)
        results=[r for r in results if r['pnu'][10]==san and int(r['pnu'][11:15])==main and int(r['pnu'][15:])==sub]
    return {'results':results[:60], 'source':EUM, 'queriedAt':now()}

def land_price(pnu):
    params = f'adm_sect_cd={pnu[:5]}|land_loc_cd={pnu[5:10]}|ledg_gbn={pnu[10]}|bobn={pnu[11:15]}|bubn={pnu[15:]}'
    raw = fetch('https://api.eum.go.kr/web/api/korep/UrlConnector.jsp?url='+params)
    root = ET.fromstring(raw)
    if root.findtext('.//CODE') != '0000':
        return {'price':None, 'pricePeriod':'', 'priceStatus':'공시지가 제공 응답 없음'}
    value = root.findtext('.//JIGA')
    return {'price':number(value) if value is not None else None,
            'pricePeriod':root.findtext('.//BASE_YEAR','')+'/'+root.findtext('.//STDMT',''),
            'priceStatus':'조회 완료' if value else '공시지가 미제공'}

def land_detail(pnu):
    pnu_valid(pnu)
    url = EUM+'/web/ar/lu/luLandDet.jsp?mode=search&selGbn=umd&isNoScr=script&pnu='+pnu
    page = fetch(url).decode('euc-kr')
    def td(id):
        m = re.search(r'<td\b[^>]*\bid=["\']'+id+r'["\'][^>]*>(.*?)</td>',page,re.S|re.I)
        return m.group(1) if m else ''
    address_match = re.search(r'<div\b[^>]*\bid=["\']present_addr["\'][^>]*>.*?</div>(.*?)</td>',page,re.S)
    use = re.search(r'\bid=["\']present_class_val["\'][^>]*\bvalue=["\']([^"\']*)',page)
    area = number(clean(td('present_area')))
    if not area or not use or not address_match:
        raise ValueError('토지이음에서 필지 기본정보를 제공하지 않습니다. 원문에서 확인하세요.')
    zones=[]
    for id in ('present_mark1','present_mark2','present_mark3'):
        content=td(id)
        # Popup descriptions contain repeated headings and law excerpts. Read only
        # direct cell anchors; their popups start with a layer_pop div.
        depth=0
        for token in re.split(r'(<a\b[^>]*>.*?</a>|</?div\b[^>]*>)', content, flags=re.S|re.I):
            if token.lower().startswith('<div'):
                depth+=1
            elif token.lower().startswith('</div'):
                depth=max(0,depth-1)
            elif depth==0 and re.match(r'<a\b',token,re.I):
                text=clean(token)
                if text and text not in zones:
                    zones.append(text)
    result={'pnu':pnu,'address':clean(address_match.group(1)), 'use':unescape(use.group(1)),
            'area':area,'zone':', '.join(zones),'sourceUrl':url,'queriedAt':now(),
            'agri':'inside' if any('농업진흥구역' in z or '농업보호구역' in z for z in zones) else 'unknown'}
    try:
        result.update(land_price(pnu))
    except Exception:
        result.update(price=None,pricePeriod='',priceStatus='공시지가 조회 실패 · 재조회 필요')
    return result

# Only official, same-parcel map images are fetched; no arbitrary URL proxy.
MAP_CACHE = {}
def land_map(pnu, scale='auto', refresh=False):
    pnu_valid(pnu)
    if scale not in ('auto','1200','5000','10000'):
        raise ValueError('지원하지 않는 지도 축척입니다.')
    key=(pnu,scale)
    cached=MAP_CACHE.get(key)
    if not refresh and cached and time.monotonic()-cached[0]<600:
        return cached[1]
    last_error=None
    # Some parcels return no map at a requested scale. Use the official default
    # on the second attempt, with a fresh session and image key.
    for selected in ([None,'1200'] if scale=='auto' else [scale,None]):
        try:
            client=opener()
            url=EUM+'/web/ar/lu/luLandDet.jsp?mode=search&selGbn=umd&isNoScr=script&pnu='+pnu+'&_='+str(time.time_ns())
            if selected:url+='&scale='+selected
            page=fetch(url,client=client).decode('euc-kr')
            match=re.search(r"<img\b[^>]*src=[\"'](images\?key=[^\"']+)",page,re.I)
            if not match:raise ValueError('토지이음에서 해당 필지의 지도 이미지를 제공하지 않습니다.')
            path=unescape(match.group(1))
            if not re.fullmatch(r'images\?key=\d+-'+pnu,path):
                raise ValueError('지도 필지가 일치하지 않습니다. 토지이음 원문을 확인하세요.')
            body=fetch(urllib.parse.urljoin(url,path),headers={'Referer':url},client=client)
            if not (body.startswith(b'\x89PNG\r\n\x1a\n') or body.startswith((b'GIF87a',b'GIF89a',b'\xff\xd8\xff'))):
                raise ValueError('토지이음 지도 생성이 지연됩니다. 다시 불러오기를 눌러주세요.')
            if len(MAP_CACHE)>=100:MAP_CACHE.pop(next(iter(MAP_CACHE)),None)
            MAP_CACHE[key]=(time.monotonic(),body)
            return body
        except Exception as error:last_error=error
    raise last_error

GEO_CACHE = {}
GEO_SHAPES = {}
def map_version(pnu):
    page=fetch(EUM+'/web/mp/mpMapDet.jsp?pnu='+pnu+'&sId=selectaddr').decode('euc-kr')
    match=re.search(r'id="selLcstSpaceDplyDate"[^>]*>.*?<option value="(\d{8})"',page,re.S)
    if not match:raise ValueError('지적도 기준일을 확인하지 못했습니다.')
    return match.group(1)

def parcel_center(pnu,version):
    pnu_valid(pnu)
    key=(pnu,version)
    if key in GEO_CACHE:return GEO_CACHE[key]
    url='https://www.eum.ne.kr:9002/MapPlan/MapPlan?'+urllib.parse.urlencode({'req':'search','version':version,'layer':'FA','code':pnu})
    data=json.loads(fetch(url))
    features=[f for f in data.get('features',[]) if str(f.get('properties',{}).get('pnu',''))==pnu]
    GEO_SHAPES[key]=[f.get('geometry',{}) for f in features]
    pts=[]
    def visit(x):
        if isinstance(x,list) and len(x)>=2 and all(isinstance(v,(int,float)) for v in x[:2]):pts.append(x[:2])
        elif isinstance(x,list):
            for y in x:visit(y)
    for f in features:visit(f.get('geometry',{}).get('coordinates',[]))
    if not pts:raise ValueError('필지 위치 좌표가 없습니다.')
    center=((min(x[0] for x in pts)+max(x[0] for x in pts))/2,(min(x[1] for x in pts)+max(x[1] for x in pts))/2)
    if len(GEO_CACHE)>3000:GEO_CACHE.clear()
    GEO_CACHE[key]=center
    return center

def nearest_registered(pnu,address,options):
    version=map_version(pnu)
    origin=parcel_center(pnu,version)
    candidates={}
    for item in options:
        lot=str(item.get('ADDR_JIBUN',''))
        m=re.fullmatch(r'\s*(산)?\s*(\d+)(?:-(\d+))?\s*(?:번지)?',lot)
        if not m:continue
        code=pnu[:10]+('2' if m.group(1) else '1')+m.group(2).zfill(4)+(m.group(3) or '0').zfill(4)
        if code!=pnu and len(code)==19:candidates[code]=lot
    if not candidates:raise ValueError('동일 법정리·동에 대체할 한전 등록 지번이 없습니다.')
    if len(candidates)>500:raise ValueError('등록 후보가 500개를 초과해 자동 거리 비교를 완료하지 못했습니다. 한전 원문에서 확인하세요.')
    def locate(item):
        code,lot=item
        try:
            x,y=parcel_center(code,version)
            return {'pnu':code,'lot':lot,'distanceM':round(math.hypot(x-origin[0],y-origin[1]),1)}
        except Exception:return None
    with ThreadPoolExecutor(max_workers=8) as pool:found=[r for r in pool.map(locate,candidates.items()) if r]
    if not found:raise ValueError('인근 등록 지번의 위치를 확인하지 못해 대체 조회할 수 없습니다.')
    found.sort(key=lambda r:(r['distanceM'],r['pnu']))
    return found,{'scope':'동일 법정리·동의 한전 등록 지번 중 좌표 확인 후보','candidateCount':len(candidates),'locatedCount':len(found),'mapVersion':version,'distanceBasis':'필지 경계박스 중심 간 직선거리(EPSG:5179), 실제 전선 연장거리가 아님'}

def tm_to_wgs84(east,north):
    a=6378137.0; f=1/298.257222101; e=2*f-f*f; ep=e/(1-e); k=.9996
    phi0=math.radians(38)
    A=1-e/4-3*e**2/64-5*e**3/256
    M0=a*(A*phi0-(3*e/8+3*e**2/32+45*e**3/1024)*math.sin(2*phi0)+(15*e**2/256+45*e**3/1024)*math.sin(4*phi0)-35*e**3/3072*math.sin(6*phi0))
    mu=(M0+(north-2000000)/k)/(a*A); e1=(1-math.sqrt(1-e))/(1+math.sqrt(1-e))
    phi=mu+(3*e1/2-27*e1**3/32)*math.sin(2*mu)+(21*e1**2/16-55*e1**4/32)*math.sin(4*mu)+151*e1**3/96*math.sin(6*mu)+1097*e1**4/512*math.sin(8*mu)
    N=a/math.sqrt(1-e*math.sin(phi)**2);R=a*(1-e)/(1-e*math.sin(phi)**2)**1.5
    T=math.tan(phi)**2;C=ep*math.cos(phi)**2;D=(east-1000000)/(N*k)
    lat=phi-(N*math.tan(phi)/R)*(D**2/2-(5+3*T+10*C-4*C*C-9*ep)*D**4/24+(61+90*T+298*C+45*T*T-252*ep-3*C*C)*D**6/720)
    lon=math.radians(127.5)+(D-(1+2*T+C)*D**3/6+(5-2*C+28*T-3*C*C+8*ep+24*T*T)*D**5/120)/math.cos(phi)
    return math.degrees(lat),math.degrees(lon)

TERRAIN_CACHE={}
TERRAIN_LOCK=threading.Lock()
TERRAIN_LAST=0

def terrain_gradient(z,spacing=30):
    east=((z[2]+2*z[5]+z[8])-(z[0]+2*z[3]+z[6]))/(8*spacing)
    north=((z[0]+2*z[1]+z[2])-(z[6]+2*z[7]+z[8]))/(8*spacing)
    slope=math.degrees(math.atan(math.hypot(east,north)))
    aspect=(math.degrees(math.atan2(-east,-north))+360)%360 if slope>=1 else None
    return slope,aspect

def terrain_detail(pnu):
    global TERRAIN_LAST
    pnu_valid(pnu)
    if pnu in TERRAIN_CACHE:return TERRAIN_CACHE[pnu]
    version=map_version(pnu);x,y=parcel_center(pnu,version);lat,lon=tm_to_wgs84(x,y)
    # Sample cardinal directions around the reference point, not TM grid north.
    lat_step=30/111320;lon_step=30/(111320*math.cos(math.radians(lat)))
    locations=[(lat+dy*lat_step,lon+dx*lon_step) for dy in (1,0,-1) for dx in (-1,0,1)]
    url='https://api.opentopodata.org/v1/srtm30m?'+urllib.parse.urlencode({'locations':'|'.join(f'{a:.7f},{b:.7f}' for a,b in locations),'interpolation':'bilinear'})
    with TERRAIN_LOCK:
        wait=1.05-(time.monotonic()-TERRAIN_LAST)
        if wait>0:time.sleep(wait)
        TERRAIN_LAST=time.monotonic()
        data=json.loads(fetch(url))
    results=data.get('results',[])
    if data.get('status')!='OK' or len(results)!=9 or any(r.get('elevation') is None or r.get('dataset')!='srtm30m' for r in results):
        raise ValueError('표고자료가 없거나 조회 한도에 도달했습니다. 잠시 후 재조회하세요.')
    heights=[float(r['elevation']) for r in results]
    slope,aspect=terrain_gradient(heights)
    dirs=['북','북동','동','남동','남','남서','서','북서']
    result={'status':'ok','pnu':pnu,'slope':round(slope,1),'aspect':round(aspect,1) if aspect is not None else None,'direction':dirs[int((aspect+22.5)//45)%8] if aspect is not None else '평탄·방향 불명확','elevation':round(heights[4],1),'latitude':lat,'longitude':lon,'source':'Open Topo Data · SRTM 30m','sourceUrl':'https://www.opentopodata.org/datasets/srtm/','queriedAt':now(),'mapVersion':version,'method':'필지 경계박스 중심 주변 60×60m 9점, 30m 간격·Horn 기울기','limitation':'필지 전체 평균·최대경사가 아닌 대표점 주변 추정. SRTM은 2000년 관측 기반 약 30m 자료로 수목·건물·절성토·좁은 필지에 오차가 큽니다. 인허가·복구비 확정 및 발전손실 자동 산정에는 사용하지 않습니다.'}
    if len(TERRAIN_CACHE)>500:TERRAIN_CACHE.clear()
    TERRAIN_CACHE[pnu]=result
    return result

def ring_inside(p,ring):
    inside=False
    for a,b in zip(ring,ring[1:]+ring[:1]):
        if (a[1]>p[1])!=(b[1]>p[1]) and p[0]<(b[0]-a[0])*(p[1]-a[1])/(b[1]-a[1])+a[0]:inside=not inside
    return inside

def point_segment(p,a,b):
    dx=b[0]-a[0];dy=b[1]-a[1];den=dx*dx+dy*dy
    t=max(0,min(1,((p[0]-a[0])*dx+(p[1]-a[1])*dy)/den)) if den else 0
    return math.hypot(p[0]-a[0]-t*dx,p[1]-a[1]-t*dy)

def segments_cross(a,b,c,d):
    def side(a,b,c):return (b[0]-a[0])*(c[1]-a[1])-(b[1]-a[1])*(c[0]-a[0])
    return side(a,b,c)*side(a,b,d)<0 and side(c,d,a)*side(c,d,b)<0

def shape_distance(points,polygons):
    if not points:return None
    edges=list(zip(points,points[1:]+points[:1])) if len(points)>1 else [(points[0],points[0])]
    best=float('inf')
    for rings in polygons:
        if not rings:continue
        if any(ring_inside(pt,rings[0]) and not any(ring_inside(pt,hole) for hole in rings[1:]) for pt in points):return 0
        if len(points)>2 and any(ring_inside(pt,points) for pt in rings[0]):return 0
        for ring in rings:
            for a,b in zip(ring,ring[1:]+ring[:1]):
                for c,d in edges:
                    if segments_cross(a,b,c,d):return 0
                    best=min(best,point_segment(c,a,b),point_segment(d,a,b),point_segment(a,c,d),point_segment(b,c,d))
    return best

HOUSING_CACHE={}
def housing_detail(pnu):
    pnu_valid(pnu)
    if pnu in HOUSING_CACHE:return HOUSING_CACHE[pnu]
    version=map_version(pnu);x,y=parcel_center(pnu,version);lat,lon=tm_to_wgs84(x,y)
    def local(a,b):return ((b-lon)*111320*math.cos(math.radians(lat)),(a-lat)*111320)
    polygons=[]
    for g in GEO_SHAPES.get((pnu,version),[]):
        parts=[g['coordinates']] if g.get('type')=='Polygon' else g.get('coordinates',[]) if g.get('type')=='MultiPolygon' else []
        for rings in parts:
            polygons.append([[local(*tm_to_wgs84(*pt[:2])) for pt in ring] for ring in rings])
    if not polygons:raise ValueError('필지 경계가 없어 200m 범위를 계산할 수 없습니다.')
    coords=[pt for rings in polygons for ring in rings for pt in ring]
    r=max(math.hypot(*pt) for pt in coords)+205
    if r>5000:raise ValueError('필지가 너무 커 자동 주거지 조회 범위를 초과했습니다.')
    lat_pad=r/111320;lon_pad=r/(111320*math.cos(math.radians(lat)))
    bbox=f'{lon-lon_pad},{lat-lat_pad},{lon+lon_pad},{lat+lat_pad}'
    root=ET.fromstring(fetch('https://api.openstreetmap.org/api/0.6/map?bbox='+bbox))
    nodes={n.get('id'):{'lat':float(n.get('lat')),'lon':float(n.get('lon'))} for n in root.findall('node')}
    ways={}
    elements=[]
    for way in root.findall('way'):
        ways[way.get('id')]=[nodes[n.get('ref')] for n in way.findall('nd') if n.get('ref') in nodes]
    for el in root:
        tags={t.get('k'):t.get('v') for t in el.findall('tag')}
        if 'building' not in tags:continue
        row={'type':el.tag,'id':el.get('id'),'tags':tags}
        if el.tag=='node':row.update(nodes[el.get('id')])
        elif el.tag=='way':row['geometry']=ways.get(el.get('id'),[])
        elif el.tag=='relation':row['members']=[{'type':m.get('type'),'ref':m.get('ref'),'role':m.get('role'),'geometry':ways.get(m.get('ref'),[])} for m in el.findall('member') if m.get('type')=='way']
        else:continue
        elements.append(row)
    data={'osm3s':{'timestamp_osm_base':root.find('meta').get('osm_base','') if root.find('meta') is not None else ''}}
    # Relation member ways represent one mapped building, not extra buildings.
    members={m['ref'] for el in elements if el.get('type')=='relation' for m in el.get('members',[]) if m.get('type')=='way'}
    residential={'apartments','house','residential','detached','semidetached_house','terrace','bungalow','dormitory','farm','static_caravan'}
    records=[];missing=0
    for el in elements:
        if el.get('type')=='way' and el.get('id') in members:continue
        tags=el.get('tags',{});parts=[]
        if el.get('type')=='node':parts=[[{'lat':el['lat'],'lon':el['lon']}]]
        elif el.get('type')=='way':parts=[el.get('geometry',[])]
        else:parts=[m.get('geometry',[]) for m in el.get('members',[]) if m.get('role')!='inner']
        distances=[shape_distance([local(pt['lat'],pt['lon']) for pt in part if 'lat' in pt and 'lon' in pt],polygons) for part in parts if part]
        distances=[d for d in distances if d is not None]
        if not distances:missing+=1;continue
        distance=min(distances)
        if distance>200:continue
        use=tags.get('building:use',tags.get('building','yes'))
        category='residential' if use in residential else 'unknown' if use in ('yes','unknown','building') or tags.get('abandoned')=='yes' or tags.get('disused')=='yes' else 'other'
        records.append({'id':str(el['id']),'type':el['type'],'category':category,'building':use,'distanceM':round(distance,1),'name':tags.get('name',''),'url':'https://www.openstreetmap.org/'+el['type']+'/'+str(el['id'])})
    records.sort(key=lambda r:r['distanceM'])
    result={'status':'ok','pnu':pnu,'radiusM':200,'residentialCount':sum(r['category']=='residential' for r in records),'unknownCount':sum(r['category']=='unknown' for r in records),'otherCount':sum(r['category']=='other' for r in records),'missingGeometry':missing,'householdCount':None,'records':records,'queriedAt':now(),'dataTimestamp':data.get('osm3s',{}).get('timestamp_osm_base',''),'source':'© OpenStreetMap contributors · OSM Map API','sourceUrl':'https://www.openstreetmap.org/copyright','method':'필지 경계와 건물 도형 사이 최소 직선거리 200m 이내 · 근사 지역 평면좌표','limitation':'위성사진 자동 판독 결과가 아닌 공개 건물도형·태그 집계입니다. 미등록·용도누락·현황변경이 있어 전체 주거지·세대 수를 확정할 수 없습니다. 0동이어도 주거지가 없다는 뜻이 아니며 이격기준 충족으로 판정하지 않습니다.'}
    if len(HOUSING_CACHE)>200:HOUSING_CACHE.clear()
    HOUSING_CACHE[pnu]=result
    return result

def union_parcel_polygons(polygons):
    try:
        from shapely.geometry import Polygon
        from shapely import union_all
    except ImportError:
        import sys
        vendor=ROOT/'runtime'/'site-packages'
        if vendor.is_dir():sys.path.insert(0,str(vendor))
        try:
            from shapely.geometry import Polygon
            from shapely import union_all
        except ImportError:raise ValueError('통합 경계 연산 라이브러리가 없습니다. 업데이트 파일과 requirements.txt를 함께 적용하세요.')
    shapes=[]
    for rings in polygons:
        if not rings:continue
        shape=Polygon(rings[0],rings[1:])
        if shape.is_empty or not shape.is_valid:raise ValueError('필지 경계가 유효하지 않아 통합 배치를 중단했습니다. 원문 경계를 확인하세요.')
        shapes.append(shape)
    if not shapes:raise ValueError('통합할 필지 경계가 없습니다.')
    merged=union_all(shapes,grid_size=.001)
    if merged.is_empty or merged.geom_type not in ('Polygon','MultiPolygon'):raise ValueError('통합 부지 경계를 만들지 못했습니다.')
    parts=[merged] if merged.geom_type=='Polygon' else list(merged.geoms)
    return [[[list(pt) for pt in part.exterior.coords]]+[[list(pt) for pt in ring.coords] for ring in part.interiors] for part in parts]

LAYOUT_CACHE={}

def satellite_tiles(bbox, service):
    radius=20037508.342789244
    xmin,ymin,xmax,ymax=bbox
    zoom=max(0,min(19,int(math.floor(math.log2(2*radius*800/((xmax-xmin)*256))))))
    span=2*radius/(2**zoom)
    c0=int(math.floor((xmin+radius)/span));c1=int(math.floor((xmax+radius)/span))
    r0=int(math.floor((radius-ymax)/span));r1=int(math.floor((radius-ymin)/span))
    tiles=[]
    for row in range(r0,r1+1):
        for col in range(c0,c1+1):
            tx=-radius+col*span;ty=radius-row*span
            tiles.append({'url':f'{service}/tile/{zoom}/{row}/{col}','x':(tx-xmin)/(xmax-xmin)*1200,'y':(ymax-ty)/(ymax-ymin)*900,'width':span/(xmax-xmin)*1200,'height':span/(ymax-ymin)*900})
    if len(tiles)>36:raise ValueError('위성 타일 범위를 초과했습니다.')
    sample=fetch(tiles[len(tiles)//2]['url'])
    if not sample.startswith((b'\xff\xd8',b'\x89PNG')):raise ValueError('위성 타일을 불러오지 못했습니다. 잠시 후 다시 시도하세요.')
    return tiles
def shading_spacing(module_length, tilt, tiers, terrains):
    if tilt not in (10,15) or tiers not in (1,2):raise ValueError('모듈각도는 10° 또는 15°, 단수는 1 또는 2로 선택하세요.')
    alpha=math.radians(25);beta=math.radians(tilt)
    length=module_length/1000*tiers+.01*(tiers-1)
    depth=length*math.cos(beta);height=length*math.sin(beta)
    gradients=[]
    for t in terrains:
        slope=t.get('slope');aspect=t.get('aspect')
        if t.get('status')!='ok' or not isinstance(slope,(float,int)) or not math.isfinite(slope) or not 0<=slope<90:
            raise ValueError('경사 자료가 없어 음영 행 간격을 계산할 수 없습니다. 입지·계통의 경사 조회를 다시 확인하세요.')
        if aspect is None:
            # Direction is undefined below 1 degree: use worst possible northward descent.
            gradient=-math.tan(math.radians(slope))
        else:
            if not isinstance(aspect,(float,int)) or not math.isfinite(aspect):raise ValueError('사면 향을 확인할 수 없습니다.')
            gradient=-math.tan(math.radians(slope))*math.cos(math.radians(aspect))
        gradients.append(gradient)
    if not gradients:raise ValueError('경사 자료가 없습니다.')
    gradient=min(gradients)
    denominator=math.tan(alpha)+gradient
    if denominator<=1e-8:raise ValueError('북쪽 하향 경사가 태양 고도 25° 이상이어서 이 기준으로 무음영 행 간격을 정할 수 없습니다. 현장 설계가 필요합니다.')
    pitch=max(depth,(height+depth*math.tan(alpha))/denominator)
    return {'tiltDeg':tilt,'tiers':tiers,'solarElevationDeg':25,'tableDepthM':depth,'tableRiseM':height,'rowPitchM':pitch,'rowGapM':pitch-depth,'northSlopeDeg':math.degrees(math.atan(gradient)),'internalGapMm':10 if tiers==2 else 0,'terrainSamples':terrains}

def satellite_layout(pnu,usable=100,density=7,row_gap=1.32,module_length=2382,module_width=1134,module_wp=650,tilt=10,tiers=1,setback=2):
    if not math.isfinite(setback) or not 1<=setback<=10 or abs(setback*2-round(setback*2))>1e-8:raise ValueError('경계 이격거리는 1~10m 범위에서 0.5m 단위로 선택하세요.')
    pnus=sorted(set(pnu.split(',') if isinstance(pnu,str) else pnu))
    if not 1<=len(pnus)<=200:raise ValueError('통합 배치는 1~200필지까지 가능합니다.')
    for item in pnus:pnu_valid(item)
    pnu=pnus[0]
    if not (0<=usable<=100 and .1<=density<=100 and 0<=row_gap<=10):raise ValueError('배치 가정 범위를 확인하세요.')
    if not all(math.isfinite(v) for v in (module_length,module_width,module_wp)) or not (100<=module_length<=6000 and 100<=module_width<=6000 and 0<module_wp<=3000):raise ValueError('모듈 길이·너비는 100~6000mm, 출력은 0 초과~3000Wp로 입력하세요.')
    if tilt not in (10,15) or tiers not in (1,2):raise ValueError('모듈각도·단수 선택을 확인하세요.')
    try:
        terrains=[terrain_detail(item) for item in pnus]
    except Exception:raise ValueError('부지 경사·사면 향 조회에 실패해 음영 배치를 중단했습니다. 잠시 후 다시 그리기를 눌러주세요.') from None
    spacing=shading_spacing(module_length,tilt,tiers,terrains)
    cache_key=(tuple(pnus),usable,density,module_length,module_width,module_wp,tilt,tiers,setback,tuple((t['slope'],t.get('aspect')) for t in terrains),'shade-setback-v1')
    if cache_key in LAYOUT_CACHE:return LAYOUT_CACHE[cache_key]
    version=map_version(pnu)
    polygons=[]
    for item in pnus:
        parcel_center(item,version)
        geometries=GEO_SHAPES.get((item,version),[])
        if not geometries:raise ValueError('일부 필지의 경계가 없어 전체 통합 배치를 중단했습니다.')
        for g in geometries:
            polygons.extend([g['coordinates']] if g.get('type')=='Polygon' else g.get('coordinates',[]) if g.get('type')=='MultiPolygon' else [])
    if len(pnus)>1:polygons=union_parcel_polygons(polygons)
    coords=[pt for rings in polygons for ring in rings for pt in ring]
    if not coords:raise ValueError('배치할 필지 경계를 찾지 못했습니다.')
    xmin=min(p[0] for p in coords);xmax=max(p[0] for p in coords);ymin=min(p[1] for p in coords);ymax=max(p[1] for p in coords)
    def ring_area(r):return abs(sum(a[0]*b[1]-b[0]*a[1] for a,b in zip(r,r[1:]+r[:1]))/2)
    area=sum(ring_area(rings[0])-sum(ring_area(r) for r in rings[1:]) for rings in polygons)
    beta=math.radians(tilt)
    module_depth=module_length/1000*math.cos(beta)
    max_tables=int(area*usable/100/((module_width/1000)*spacing['tableDepthM']))
    w=module_width/1000;h=spacing['tableDepthM'];edge=setback;pitchx=w+.02;pitchy=spacing['rowPitchM']
    if ((xmax-xmin)/pitchx)*((ymax-ymin)/pitchy)>120000:raise ValueError('배치 범위가 너무 커 자동배치 한도를 초과했습니다.')
    boundary=[(a,b) for rings in polygons for ring in rings for a,b in zip(ring,ring[1:]+ring[:1])]
    def contained(rect):
        if not all(any(ring_inside(p,rings[0]) and not any(ring_inside(p,r) for r in rings[1:]) for rings in polygons) for p in rect):return False
        for a,b in zip(rect,rect[1:]+rect[:1]):
            for c,d in boundary:
                if segments_cross(a,b,c,d) or min(point_segment(a,c,d),point_segment(b,c,d),point_segment(c,a,b),point_segment(d,a,b))<edge-1e-7:return False
        # Reject a courtyard/hole entirely enclosed by a module.
        return not any(ring_inside(r[0],rect) for rings in polygons for r in rings[1:] if r)
    slots=[];y=ymin+edge
    while y+h<=ymax-edge+1e-8:
        x=xmin+edge
        while x+w<=xmax-edge+1e-8:
            rect=[[x,y],[x+w,y],[x+w,y+h],[x,y+h]]
            if contained(rect):slots.append(rect)
            x+=pitchx
        y+=pitchy
    table_count=min(len(slots),max_tables,15000//tiers)
    modules=[]
    for rect in slots[:table_count]:
        x,y=rect[0]
        for tier in range(tiers):
            py=y+tier*(module_depth+.01*math.cos(beta))
            modules.append([[x,py],[x+w,py],[x+w,py+module_depth],[x,py+module_depth]])
    n=len(modules)
    row_gap=spacing['rowGapM']
    def merc(pt):
        lat,lon=tm_to_wgs84(*pt[:2]);return [6378137*math.radians(lon),6378137*math.log(math.tan(math.pi/4+math.radians(lat)/2))]
    outlines=[[[merc(pt) for pt in ring] for ring in rings] for rings in polygons]
    panels=[[merc(pt) for pt in rect] for rect in modules]
    allpts=[pt for rings in outlines for ring in rings for pt in ring]
    mx0=min(p[0] for p in allpts);mx1=max(p[0] for p in allpts);my0=min(p[1] for p in allpts);my1=max(p[1] for p in allpts)
    cx=(mx0+mx1)/2;cy=(my0+my1)/2;span=max(mx1-mx0,(my1-my0)*4/3,300)*1.2
    bbox=[cx-span/2,cy-span*3/8,cx+span/2,cy+span*3/8]
    service='https://services.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer'
    tiles=[]
    try:
        export=json.loads(fetch(service+'/export?'+urllib.parse.urlencode({'bbox':','.join(str(v) for v in bbox),'bboxSR':3857,'imageSR':3857,'size':'800,600','format':'jpg','f':'json'})))
    except Exception:
        export={}
    if export.get('href') and export.get('extent'):
        if urllib.parse.urlparse(export['href']).hostname!='services.arcgisonline.com':raise ValueError('위성영상 출처 주소를 확인하지 못했습니다.')
        ex=export['extent'];extent=[ex[k] for k in ('xmin','ymin','xmax','ymax')]
    else:
        tiles=satellite_tiles(bbox,service)
        extent=bbox
        export={'href':''}
    def pixel(pt):return [round((pt[0]-extent[0])/(extent[2]-extent[0])*1200,3),round((extent[3]-pt[1])/(extent[3]-extent[1])*900,3)]
    result={'status':'ok','pnu':pnu,'pnus':pnus,'parcelCount':len(pnus),'combined':len(pnus)>1,'count':n,'capacityKw':round(n*module_wp/1000,3),'slotCount':len(slots),'moduleWp':module_wp,'moduleLengthMm':module_length,'moduleWidthMm':module_width,'boundarySetbackMm':setback*1000,'rowGapM':row_gap,'moduleGapMm':20,'usablePct':usable,'density':density,'area':round(area,1),'mapVersion':version,'imageUrl':export['href'],'extent':extent,'outlines':[[[pixel(pt) for pt in ring] for ring in rings] for rings in outlines],'panels':[[pixel(pt) for pt in rect] for rect in panels],'queriedAt':now(),'imagerySource':service,'attribution':'Tiles © Esri — Source: Esri, Vantor, Earthstar Geographics, and the GIS User Community','limitation':f'부지 경계 안쪽 {setback:g}m·행 간격 가정·수평 평면 배치. 주거지 규제·진입로·지장물·지형음영·경사 투영·구조설계는 미반영. 영상 촬영일 미확인, 최신 현황·정밀측량 확인 필요. 선택 필지의 합집합 경계로 통합 배치하며 공유 경계에는 이격을 두지 않습니다. 떨어진 필지·제외된 땅·내부 구멍은 연결하거나 채우지 않으며 실제 외곽 및 내부 빈 땅 경계에서 {setback:g}m를 유지합니다. 경계 연산 정밀도 1mm.'}
    result['imageTiles']=tiles
    result['imageryMode']='tiles' if tiles else 'export'
    result['spacing']=spacing
    result['tableCount']=table_count
    result['cadGeometry']={'crs':'EPSG:5179','units':'m','outlines':polygons,'panels':modules,'moduleWp':module_wp,'moduleLengthMm':module_length,'moduleWidthMm':module_width,'tiltDeg':tilt,'tiers':tiers,'rowGapM':row_gap,'rowPitchM':spacing['rowPitchM'],'boundarySetbackM':setback}
    result['cadToken']=secrets.token_urlsafe(24)
    result['limitation']=f'정남향·수평면 기준 모듈각도·태양 고도 25°의 남북 단면 개념 배치. DEM 대표 경사·사면 향 중 가장 불리한 남북 기울기를 전체에 적용합니다. 국부 지형·동서 방향 음영·아침저녁 태양·주변 장애물·구조물 높이 차·정밀 지형은 미반영이며 연중 무음영을 보장하지 않습니다. 통합 부지 외곽과 내부 빈 땅에서 {setback:g}m 이격합니다. 2단은 같은 경사면에 세로 모듈 2장을 연속 설치하고 내부 틈 1cm를 둡니다.'
    if len(LAYOUT_CACHE)>100:LAYOUT_CACHE.clear()
    LAYOUT_CACHE[cache_key]=result
    return result

def kepco_post(action, data, client):
    raw=fetch(KEPCO+'/ew/cpct/'+action,json.dumps(data,ensure_ascii=False).encode(),
              {'Content-Type':'application/json;charset=UTF-8','Referer':GRID_PAGE},client)
    return json.loads(raw.decode('utf-8'))

def grid_detail(address,pnu):
    pnu_valid(pnu)
    client=opener()
    fetch(GRID_PAGE,client=client)
    choices=kepco_post('retrieveAddrInit',{},client).get('dlt_sido',[])
    tokens=address.split()
    aliases={'서울시':'서울특별시','부산시':'부산광역시','대구시':'대구광역시','인천시':'인천광역시',
             '광주시':'광주광역시','대전시':'대전광역시','울산시':'울산광역시','세종시':'세종특별자치시',
             '강원도':'강원특별자치도','전라북도':'전북특별자치도','제주도':'제주특별자치도'}
    tokens=[aliases.get(t,t) for t in tokens]
    def choose(items,key,optional=False):
        values=[str(item.get(key,'')) for item in items]
        match=[v for v in values if v in tokens]
        if len(match)==1:return match[0]
        if '-기타지역' in values:return '-기타지역'
        if optional and (not values or values==['']):return ''
        raise ValueError('한전 주소 목록에 정확한 행정구역이 없습니다. 한전 원문에서 확인하세요.')
    province=choose(choices,'ADDR_DO')
    params={'addr_do':province}
    sequence=[('addr_si','ADDR_SI'),('addr_gu','ADDR_GU'),('addr_lidong','ADDR_LIDONG'),('addr_li','ADDR_LI'),('addr_jibun','ADDR_JIBUN')]
    substitution=None
    ranked=[]
    nearest_meta={}
    gbn=0
    while gbn<len(sequence):
        payload=kepco_post('retrieveAddrGbn',{'dma_addrGbn':{**params,'gbn':str(gbn)}},client)
        options=payload.get('dlt_addrGbn',[])
        available=[i for i,(_,column) in enumerate(sequence) if any(column in x for x in options)]
        if not available:
            return {'status':'not_found','message':'한전 주소 목록에 동일 필지가 없습니다.', 'rows':[], 'sourceUrl':GRID_PAGE,'queriedAt':now(),'address':address}
        actual=available[0]
        if actual<gbn:raise ValueError('한전 주소 응답 형식이 변경되었습니다.')
        for skipped in range(gbn,actual):params[sequence[skipped][0]]=''
        gbn=actual
        key,col=sequence[gbn]
        if key=='addr_jibun':
            desired=('산' if pnu[10]=='2' else '')+str(int(pnu[11:15]))+('-'+str(int(pnu[15:])) if int(pnu[15:]) else '')
            def norm(v):
                return re.sub(r'\s|번지','',str(v)).replace('-0','')
            matches=[str(x.get(col,'')) for x in options if norm(x.get(col,''))==desired]
            if len(matches)!=1:
                try:
                    ranked,nearest_meta=nearest_registered(pnu,address,options)
                except Exception as error:
                    return {'status':'not_found','message':'동일 지번 미등록 · 인근 대체 조회 실패: '+str(error),'rows':[],'sourceUrl':GRID_PAGE,'queriedAt':now(),'address':address}
                substitution=ranked[0]
                params[key]=substitution['lot']
            else:params[key]=matches[0]
        else:
            params[key]=choose(options,col,optional=key in ('addr_gu','addr_li'))
        gbn+=1
    req={'searchCondition':'address', **{k:params['addr_'+k] for k in ('do','si','gu','lidong','li','jibun')}}
    req['gu']='' if req['gu']=='-기타지역' else req['gu']
    req['li']='' if req['li']=='-기타지역' else req['li']
    payload=kepco_post('retrieveMeshNo',{'dma_reqParam':req},client)
    if not substitution and not payload.get('dlt_resultList'):
        try:
            ranked,nearest_meta=nearest_registered(pnu,address,options)
            substitution=ranked[0]
            req['jibun']=substitution['lot']
            payload=kepco_post('retrieveMeshNo',{'dma_reqParam':req},client)
        except Exception as error:
            return {'status':'not_found','rows':[],'sourceUrl':GRID_PAGE,'queriedAt':now(),'address':address,'message':'동일 지번 선로 미제공 · 인근 대체 조회 실패: '+str(error)}
    if substitution:
        # A registered lot may still have no circuit result; try in measured order.
        for candidate in ranked:
            req['jibun']=candidate['lot']
            if candidate!=substitution:payload=kepco_post('retrieveMeshNo',{'dma_reqParam':req},client)
            if payload.get('dlt_resultList'):
                substitution=candidate
                break
        else:substitution=None
    rows=[]
    for row in payload.get('dlt_resultList',[]):
        levels=[]
        for label,cap,used,planned in [('변전소','SUBST_CAPA','SUBST_PWR','G_SUBST_CAPA'),('주변압기','MTR_CAPA','MTR_PWR','G_MTR_CAPA'),('배전선로','DL_CAPA','DL_PWR','G_DL_CAPA')]:
            c,u,g=number(row.get(cap,'')),number(row.get(used,'')),number(row.get(planned,''))
            levels.append({'label':label,'capacity':c,'received':u,'planned':g,
                           'available':c-u if c is not None and u is not None else None,
                           'planAvailable':c-g if c is not None and g is not None else None})
        rows.append({'substation':row.get('SUBST_NM',''),'transformer':str(row.get('MTR_NO','')),
                     'line':row.get('DL_NM',''),'substationCode':row.get('SUBST_CD',''),
                     'lineCode':row.get('DL_CD',''),'levels':levels})
    return {'status':'ok' if rows else 'not_found','rows':rows,'sourceUrl':GRID_PAGE,'queriedAt':now(),
            'address':address,'queryAddress':req,'substituted':bool(substitution),'substitution':({**substitution,**nearest_meta,'matchedAddress':address.rsplit(' ',1)[0]+' '+substitution['lot']} if substitution else None),'message':'특고압(22.9kV) 기준 참고자료. 송전망 여유·최종 접속 검토는 별도입니다.'}

class Handler(SimpleHTTPRequestHandler):
    def do_POST(self):
        if self.headers.get('Host','').split(':')[0] not in ('127.0.0.1','localhost'):return self.reply({'error':'Local access only'},403)
        if urllib.parse.urlparse(self.path).path!='/api/cad-package':return self.reply({'error':'Unknown endpoint'},404)
        origin=self.headers.get('Origin')
        if origin and origin!='http://'+self.headers.get('Host',''):return self.reply({'error':'허용되지 않은 요청입니다.'},403)
        try:
            size=int(self.headers.get('Content-Length','0'))
            if not 0<size<=4096:return self.reply({'error':'잘못된 다운로드 요청입니다.'},400)
            import cad_export
            body=cad_export.prepare(json.loads(self.rfile.read(size).decode()))
            self.send_response(200);self.send_header('Content-Type','application/zip');self.send_header('Content-Length',str(len(body)));self.send_header('Content-Disposition','attachment; filename="SOLAR_JIN_CAD_satellite.zip"');self.end_headers();self.wfile.write(body)
        except ValueError as error:return self.reply({'error':str(error)},422)
        except Exception:return self.reply({'error':'위성사진 CAD 작성에 실패했습니다. 잠시 후 다시 시도하세요.'},502)
    def __init__(self,*a,**kw):super().__init__(*a,directory=str(ROOT/'dist'),**kw)
    def log_message(self,*args):pass  # Do not record parcel queries on disk.
    def reply(self,value,status=200):
        b=json.dumps(value,ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type','application/json;charset=utf-8')
        self.send_header('Cache-Control','no-store')
        self.send_header('Access-Control-Allow-Origin','null')
        self.end_headers();self.wfile.write(b)
    def do_GET(self):
        host=self.headers.get('Host','').split(':')[0]
        if host not in ('127.0.0.1','localhost'):
            return self.reply({'error':'Local access only'},403)
        url=urllib.parse.urlparse(self.path);q=urllib.parse.parse_qs(url.query)
        try:
            if url.path=='/api/health':return self.reply({'ok':True,'app':'SOLAR_JIN','version':2,'instance':hashlib.sha256(str(ROOT).encode()).hexdigest()[:16]})
            if url.path=='/api/map':
                body=land_map(q.get('pnu',[''])[0],q.get('scale',['auto'])[0],refresh='retry' in q)
                self.send_response(200)
                self.send_header('Content-Type','image/png' if body.startswith(b'\x89PNG') else 'image/gif' if body.startswith(b'GIF') else 'image/jpeg')
                self.send_header('Cache-Control','private, max-age=600')
                self.send_header('Content-Length',str(len(body)))
                self.end_headers();self.wfile.write(body)
                return
            if url.path=='/api/search':return self.reply(search_land(q.get('q',[''])[0]))
            if url.path=='/api/land':return self.reply(land_detail(q.get('pnu',[''])[0]))
            if url.path=='/api/layout':return self.reply(satellite_layout(q.get('pnus',q.get('pnu',['']))[0],float(q.get('usable',['100'])[0]),float(q.get('density',['7'])[0]),float(q.get('rowGap',['1.32'])[0]),float(q.get('moduleLength',['2382'])[0]),float(q.get('moduleWidth',['1134'])[0]),float(q.get('moduleWp',['650'])[0]),float(q.get('tilt',['10'])[0]),int(q.get('tiers',['1'])[0]),float(q.get('setback',['2'])[0])))
            if url.path=='/api/housing':return self.reply(housing_detail(q.get('pnu',[''])[0]))
            if url.path=='/api/terrain':return self.reply(terrain_detail(q.get('pnu',[''])[0]))
            if url.path=='/api/grid':
                # Derive official address from PNU; never accept arbitrary target URLs.
                land=land_detail(q.get('pnu',[''])[0])
                return self.reply(grid_detail(land['address'],land['pnu']))
            if url.path.startswith('/api/'):return self.reply({'error':'Unknown endpoint'},404)
            return super().do_GET()
        except ValueError as e:return self.reply({'error':str(e)},422)
        except Exception:return self.reply({'error':'공식 조회 서비스 연결에 실패했습니다. 잠시 후 재조회하거나 원문을 확인하세요.'},502)

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--port',type=int,default=8765)
    args=parser.parse_args()
    server=ThreadingHTTPServer(('127.0.0.1',args.port),Handler)
    print(f'SOLAR_JIN http://127.0.0.1:{args.port}',flush=True)
    server.serve_forever()
