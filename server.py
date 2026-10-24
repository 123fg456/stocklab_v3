"""STOCK LAB v3: licensed data adapters, OpenDART, strict coverage scoring."""
import os, re, math, json, datetime, urllib.request, urllib.parse, io, csv
from zoneinfo import ZoneInfo
from flask import Flask, jsonify, request, send_from_directory
app=Flask(__name__,static_folder='.')
KST=ZoneInfo('Asia/Seoul')
DART='https://opendart.fss.or.kr/api/'

def fetch_json(url, headers=None):
 req=urllib.request.Request(url,headers={'User-Agent':'StockLab/3.0 (data verification)'}| (headers or {}))
 with urllib.request.urlopen(req,timeout=18) as r:return json.load(r)

def provider(path,params):
 base=os.getenv('MARKET_DATA_BASE_URL','').strip().rstrip('/')
 if not base:return None
 if not base.startswith('https://'):raise ValueError('MARKET_DATA_BASE_URL은 HTTPS 주소여야 합니다.')
 url=base+'/'+path+'?'+urllib.parse.urlencode(params)
 key=os.getenv('MARKET_DATA_API_KEY','').strip()
 return fetch_json(url,{'Authorization':'Bearer '+key} if key else {})

def ticker(q):
 q=q.strip()
 if re.fullmatch(r'\d{6}',q):return q,q
 listing=provider('symbols',{'q':q})
 if not listing:raise ValueError('종목명 검색은 허가된 시세 API 연결 후 가능합니다. 지금은 6자리 종목코드를 입력하세요.')
 matches=[x for x in listing.get('items',[]) if x.get('name')==q]
 if len(matches)!=1:raise ValueError('종목명에 해당하는 고유 종목을 찾지 못했습니다. 6자리 코드를 입력하세요.')
 return matches[0]['code'],matches[0]['name']

def validate_bars(raw):
 rows=raw.get('items',[]) if isinstance(raw,dict) else []
 out=[];seen=set()
 for r in rows:
  try:
   d=datetime.date.fromisoformat(r['date']);close=float(r['close']);volume=int(r['volume']);hi=float(r.get('high',close));lo=float(r.get('low',close))
   if d>datetime.datetime.now(KST).date() or close<=0 or volume<=0 or lo>hi or close>hi*1.03 or close<lo*.97 or d.isoformat() in seen:continue
   seen.add(d.isoformat());out.append({'date':d.isoformat(),'close':close,'volume':volume,'high':hi,'low':lo})
  except (ValueError,TypeError,KeyError):continue
 out.sort(key=lambda x:x['date'])
 return out

def ma(rows,n):return round(sum(x['close'] for x in rows[-n:])/n,2) if len(rows)>=n else None

def rsi(rows,n=14):
 if len(rows)<n+1:return None
 diff=[rows[i]['close']-rows[i-1]['close'] for i in range(1,len(rows))]
 gain=sum(max(v,0) for v in diff[:n])/n;loss=sum(max(-v,0) for v in diff[:n])/n
 for v in diff[n:]:gain=(gain*(n-1)+max(v,0))/n;loss=(loss*(n-1)+max(-v,0))/n
 return round(100 if loss==0 else 100-100/(1+gain/loss),2)

def score_technical(rows):
 if len(rows)<120:return None
 c=rows[-1]['close'];a=ma(rows,20);b=ma(rows,60);d=ma(rows,120);r=rsi(rows)
 return (7 if c>a else 0)+(7 if c>b else 0)+(3 if c>d else 0)+(3 if r is not None and 40<=r<=68 else 0)

def validate_flows(raw,bars):
 if not raw:return None,'수급 제공처가 설정되지 않았습니다.'
 rows=[];volume={x['date']:x['volume'] for x in bars}
 for r in raw.get('items',[]):
  try:
   date=datetime.date.fromisoformat(r['date']).isoformat();f=int(r['foreign_net_shares']);i=int(r['institution_net_shares'])
   if date in volume and abs(f)<=volume[date]*2 and abs(i)<=volume[date]*2:rows.append({'date':date,'foreign':f,'institution':i,'volume':volume[date]})
  except (KeyError,ValueError,TypeError):continue
 rows.sort(key=lambda x:x['date']);rows=list({x['date']:x for x in rows}.values());rows.sort(key=lambda x:x['date'])
 if len(rows)<60:return None,f'수급 일치 거래일 부족: {len(rows)}/60'
 if rows[-1]['date']!=bars[-1]['date']:return None,'주가·수급 최종 기준일 불일치'
 return rows,None

def flow_score(rows,bars):
 if rows is None:return None
 score=0;periods={};changes={}
 for n in (5,20,60):
  p=rows[-n:];vol=sum(x['volume'] for x in p)
  periods[str(n)]={'foreign':sum(x['foreign'] for x in p),'institution':sum(x['institution'] for x in p),'foreign_strength':round(100*sum(x['foreign'] for x in p)/vol,3),'institution_strength':round(100*sum(x['institution'] for x in p)/vol,3),'joint_days':sum(x['foreign']>0 and x['institution']>0 for x in p),'foreign_days':sum(x['foreign']>0 for x in p),'institution_days':sum(x['institution']>0 for x in p)}
 for actor in ('foreign','institution'):
  strength=periods['20'][actor+'_strength'];days=periods['20'][actor+'_days'];short=periods['5'][actor+'_strength'];long=periods['60'][actor+'_strength']
  score+= (4 if strength>=2 else 3 if strength>=1 else 2 if strength>0 else 0)+(3 if days>=14 else 2 if days>=11 else 1 if days>=8 else 0)+(3 if short>0 and long>0 else 2 if short>0 else 0)
 score+=round(5*periods['20']['joint_days']/20)
 ret=bars[-1]['close']/bars[-21]['close']-1 if len(bars)>=21 else 0
 combined=periods['20']['foreign']+periods['20']['institution']
 score+=5 if combined>0 and ret>0 else 2 if combined>0 else 0
 short=periods['5']['foreign']+periods['5']['institution'];long=combined
 score+=5 if short>0 and long<=0 else 3 if short>0 and long>0 else 0
 return {'points':min(35,score),'periods':periods,'daily':rows[-40:],'date':rows[-1]['date']}

def dart_request(endpoint,params):
 key=os.getenv('DART_API_KEY','').strip()
 if not key:return None
 data=fetch_json(DART+endpoint+'?'+urllib.parse.urlencode({'crtfc_key':key}|params))
 return data if data.get('status')=='000' else {'error':data.get('message','조회 실패'),'status':data.get('status')}

def dart_disclosures(code):
 key=os.getenv('DART_API_KEY','').strip()
 if not key:return None
 # OpenDART list API accepts stock_code; some periods may have no disclosures.
 end=datetime.datetime.now(KST).date();start=end-datetime.timedelta(days=100)
 raw=dart_request('list.json',{'bgn_de':start.strftime('%Y%m%d'),'end_de':end.strftime('%Y%m%d'),'stock_code':code,'page_count':30})
 if not raw or 'error' in raw:return {'items':[],'error':(raw or {}).get('error','조회 실패')}
 return {'items':[{'title':r.get('report_nm'),'date':r.get('rcept_dt'),'url':'https://dart.fss.or.kr/dsaf001/main.do?rcpNo='+r['rcept_no']} for r in raw.get('list',[]) if r.get('rcept_no')][:20]}

def financial_data(code):
 if not os.getenv('DART_API_KEY'):return None
 # For reliable company financials, CORP_CODE mapping is required; no guessed company code.
 # This endpoint is intentionally not called without mapping.
 corp=os.getenv('DART_CORP_'+code,'')
 if not re.fullmatch(r'\d{8}',corp):return None
 year=datetime.datetime.now(KST).year
 for y in (year,year-1):
  for reprt in ('11014','11012','11013','11011'):
   raw=dart_request('fnlttSinglAcntAll.json',{'corp_code':corp,'bsns_year':y,'reprt_code':reprt,'fs_div':'CFS'})
   if not raw or 'list' not in raw:continue
   records=raw['list'];out={}
   for k,terms in {'revenue':['매출액','영업수익'],'operating_profit':['영업이익'],'net_income':['당기순이익']}.items():
    for r in records:
     if r.get('account_nm','').strip() in terms and r.get('sj_div') in ('IS','CIS'):
      try:out[k]=int(r['thstrm_amount'].replace(',',''))
      except (ValueError,KeyError,AttributeError):pass
      break
   if out:return {'year':y,'report_code':reprt,'items':out,'source':'OpenDART 연결재무제표'}
 return None

def rating(bars,flow,financial,disclosures):
 tech=score_technical(bars);parts={'technical':{'points':tech,'max':20},'flows':{'points':flow['points'] if flow else None,'max':35},'financial':{'points':None,'max':20},'market':{'points':None,'max':15},'news':{'points':None,'max':10}}
 # Financial and news retrieval is not a validated valuation/industry scoring model.
 # Do not award proxy points simply because data was retrieved.
 known=sum(v['max'] for v in parts.values() if v['points'] is not None)
 earned=sum(v['points'] for v in parts.values() if v['points'] is not None)
 return {'parts':parts,'known_points':known,'earned_points':earned,'complete':known==100,'total_score':earned if known==100 else None,'status':'평가 보류' if known<100 else '완료'}

@app.get('/')
def index():return send_from_directory(app.static_folder,'index.html')
@app.get('/<path:name>')
def static_file(name):
 if name.startswith('api/'):return jsonify(error='찾을 수 없음'),404
 return send_from_directory(app.static_folder,name)
@app.get('/health')
def health():return jsonify(status='ok',time=datetime.datetime.now(KST).isoformat(),market_provider_configured=bool(os.getenv('MARKET_DATA_BASE_URL')),dart_configured=bool(os.getenv('DART_API_KEY')))
@app.post('/api/analyze')
def analyze():
 data=request.get_json(silent=True) or {};q=str(data.get('ticker','')).strip()
 if not q or len(q)>35:return jsonify(error='6자리 종목코드 또는 종목명을 입력하세요.'),400
 try:
  code,name=ticker(q)
  raw=provider('prices',{'ticker':code,'limit':260})
  if not raw:return jsonify(error='허가된 시세 API가 설정되지 않았습니다. Render 환경변수 MARKET_DATA_BASE_URL 및 MARKET_DATA_API_KEY를 설정하세요. 임의 시세는 제공하지 않습니다.'),503
  bars=validate_bars(raw)
  if len(bars)<65:return jsonify(error=f'검증된 주가 데이터 부족: {len(bars)}/65거래일'),422
  last=bars[-1];today=datetime.datetime.now(KST).date()
  warnings=[]
  if (today-datetime.date.fromisoformat(last['date'])).days>5:warnings.append('마지막 주가 데이터가 5일 이상 오래되었습니다.')
  try:flow_raw=provider('flows',{'ticker':code,'limit':90})
  except Exception as e:flow_raw=None;warnings.append('수급 API 조회 실패: '+type(e).__name__)
  rows,flow_err=validate_flows(flow_raw,bars)
  if flow_err:warnings.append(flow_err)
  flow=flow_score(rows,bars)
  financial=financial_data(code)
  disclosures=dart_disclosures(code)
  if not financial:warnings.append('검증된 DART 재무제표 연결 또는 기업코드 매핑이 없습니다.')
  if disclosures is None:warnings.append('DART_API_KEY 미설정: 공시 조회 불가')
  score=rating(bars,flow,financial,disclosures)
  price=last['close'];a=ma(bars,20);b=ma(bars,60);c=ma(bars,120)
  support=round(min(x['low'] for x in bars[-20:]),2);resistance=round(max(x['high'] for x in bars[-60:]),2)
  target=resistance if resistance>price else None;stop=round(support*.985,2) if support<price else None
  rr=round((target-price)/(price-stop),2) if target and stop and price>stop else None
  return jsonify(ticker=code,name=raw.get('name',name),generated_at=datetime.datetime.now(KST).strftime('%Y-%m-%d %H:%M KST'),market={'date':last['date'],'price':price,'daily':bars[-90:],'ma':{'20':a,'60':b,'120':c},'rsi':rsi(bars),'support':support,'resistance':resistance,'target1':target,'stop':stop,'rr':rr},flows=flow,financial=financial,disclosures=disclosures,score=score,warnings=warnings,sources=[{'name':'허가된 시세·수급 데이터 제공처','url':raw.get('source_url') or ''},{'name':'OpenDART','url':'https://opendart.fss.or.kr/'},{'name':'KIND','url':'https://kind.krx.co.kr/'},{'name':'한국은행 ECOS','url':'https://ecos.bok.or.kr/'}])
 except ValueError as e:return jsonify(error=str(e)),400
 except Exception as e:return jsonify(error='실제 데이터 조회 실패: '+type(e).__name__+' — '+str(e)[:160]),502

if __name__=='__main__':app.run(host='0.0.0.0',port=int(os.getenv('PORT','8765')))
