"""Run an explicitly supplied command and preserve timing, command and output."""
import datetime,json,pathlib,subprocess,sys,time,os
e=pathlib.Path('C:/capstone2/docs/research/후보1-바이브코딩보안/evidence');e.mkdir(exist_ok=True,parents=True)
name=sys.argv[1];args=sys.argv[2:];start=time.monotonic();started=datetime.datetime.now(datetime.timezone.utc).isoformat()
with (e/(name+'.stdout.log')).open('w',encoding='utf-8') as out, (e/(name+'.stderr.log')).open('w',encoding='utf-8') as err:
 p=subprocess.run(args,stdout=out,stderr=err,encoding='utf-8',errors='replace')
d={'command':args,'cwd':os.getcwd(),'started_utc':started,'elapsed_seconds':round(time.monotonic()-start,3),'exit_code':p.returncode}
(e/(name+'.run.json')).write_text(json.dumps(d,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps(d,ensure_ascii=False));sys.exit(p.returncode)
