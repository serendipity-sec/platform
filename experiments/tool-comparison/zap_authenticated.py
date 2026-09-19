"""Local-container-only bearer-authenticated ZAP scan + separate manual controls.
No URL is accepted from the command line. No external hosts are contacted.
"""
import datetime,json,pathlib,time,urllib.request,urllib.parse,urllib.error,os
IN_DOCKER=os.getenv('CAPSTONE_IN_DOCKER')=='1'
OUT=pathlib.Path('/out' if IN_DOCKER else 'C:/capstone2/docs/research/후보1-바이브코딩보안/evidence')
opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
inside='http://capstone-juice:3000'
target=inside if IN_DOCKER else 'http://127.0.0.1:18780'
zap='http://capstone-zap-auth:8080' if IN_DOCKER else 'http://127.0.0.1:18781'
events=[];http=[];start=time.monotonic()
def call(base,path,body=None,headers=None):
 req=urllib.request.Request(base+path,None if body is None else json.dumps(body).encode(),{'Content-Type':'application/json',**(headers or {})})
 try:r=opener.open(req,timeout=50)
 except urllib.error.HTTPError as e:r=e
 text=r.read().decode('utf-8',errors='replace')
 try:result=json.loads(text)
 except Exception:result=text[:5000]
 return r.status,result
def api(comp,kind,method,**params):
 status,data=call(zap,f'/JSON/{comp}/{kind}/{method}/?'+urllib.parse.urlencode(params))
 events.append({'component':comp,'kind':kind,'method':method,'params':params,'status':status,'result':data})
 if status!=200:raise RuntimeError(str(data))
 return data
def manual(label,path,body=None,token=None):
 status,data=call(target,path,body,{'Authorization':'Bearer '+token} if token else None)
 http.append({'label':label,'method':'POST' if body is not None else 'GET','url':target+path,'request_json':body,'authorization':token,'status':status,'response':data})
 return data
try:
 for _ in range(90):
  try:api('core','view','version');break
  except Exception:time.sleep(1)
 api('core','action','newSession',name='',overwrite='true')
 for name in ['a','b']:
  manual('register '+name,'/api/Users/',{'email':f'capstone_{name}@example.invalid','password':'Dummy-2026!','passwordRepeat':'Dummy-2026!','securityQuestion':{'id':1},'securityAnswer':'dummy'})
 a=manual('login A','/rest/user/login',{'email':'capstone_a@example.invalid','password':'Dummy-2026!'})['authentication']
 b=manual('login B','/rest/user/login',{'email':'capstone_b@example.invalid','password':'Dummy-2026!'})['authentication']
 manual('B own basket','/rest/basket/'+str(b['bid']),token=b['token'])
 manual('A reads B basket (manual control)','/rest/basket/'+str(b['bid']),token=a['token'])
 manual('no token B basket','/rest/basket/'+str(b['bid']))
 manual('wrong password control','/rest/user/login',{'email':'capstone_a@example.invalid','password':'wrong'})
 manual('SQLi login bypass (manual control)','/rest/user/login',{'email':"' OR 1=1--",'password':'wrong'})
 api('context','action','newContext',contextName='capstone-juice-only')
 api('context','action','includeInContext',contextName='capstone-juice-only',regex=r'http://capstone-juice:3000.*')
 for rule in api('replacer','view','rules').get('rules',[]):
  if rule.get('description')=='Dummy A bearer on local target':
   api('replacer','action','removeRule',description=rule['description'])
 api('replacer','action','addRule',description='Dummy A bearer on local target',enabled='true',matchType='REQ_HEADER',matchRegex='false',matchString='Authorization',replacement='Bearer '+a['token'],url=r'http://capstone-juice:3000.*')
 seeds=[('GET','/rest/basket/'+str(a['bid']),None),('GET','/rest/basket/'+str(b['bid']),None),('GET','/rest/products/search?q=apple',None),('GET','/rest/user/whoami',None),('POST','/rest/user/login',{'email':'capstone_a@example.invalid','password':'Dummy-2026!'}),('GET','/ftp',None)]
 for method,path,body in seeds:
  text='' if body is None else json.dumps(body)
  raw=f'{method} {inside+path} HTTP/1.1\r\nHost: capstone-juice:3000\r\nAuthorization: Bearer {a["token"]}\r\nContent-Type: application/json\r\nContent-Length: {len(text.encode())}\r\n\r\n{text}'
  api('core','action','sendRequest',request=raw,followRedirects='false')
 api('ascan','action','setOptionMaxRuleDurationInMins',Integer=1)
 api('ascan','action','setOptionMaxScanDurationInMins',Integer=5)
 scan=api('ascan','action','scan',url=inside,recurse='true',inScopeOnly='true')['scan']
 deadline=time.monotonic()+370
 completed=False
 while time.monotonic()<deadline:
  status=api('ascan','view','status',scanId=scan)
  if status.get('status')=='100':completed=True;break
  time.sleep(5)
 if not completed:api('ascan','action','stop',scanId=scan)
 # Allow passive queue to catch up, bounded separately.
 for _ in range(12):
  if api('pscan','view','recordsToScan').get('recordsToScan')=='0':break
  time.sleep(2)
 alerts=api('core','view','alerts',baseurl=inside,start=0,count=10000)
 (OUT/'zap-auth-alerts.json').write_text(json.dumps(alerts,ensure_ascii=False,indent=2),encoding='utf-8')
 result={'completed':completed,'scan_id':scan,'elapsed_seconds':time.monotonic()-start,'method':'manually acquired dummy bearer token injected on allowlisted target; no automatic re-login; no multiuser oracle'}
except Exception as e:
 result={'error':repr(e),'elapsed_seconds':time.monotonic()-start};print(result)
finally:
 result['checked_utc']=datetime.datetime.now(datetime.timezone.utc).isoformat()
 (OUT/'zap-auth-run.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
 (OUT/'zap-auth-api.json').write_text(json.dumps(events,ensure_ascii=False,indent=2),encoding='utf-8')
 (OUT/'juice-manual-controls.json').write_text(json.dumps(http,ensure_ascii=False,indent=2),encoding='utf-8')
 print(json.dumps(result))
