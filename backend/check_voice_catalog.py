import sys,json,asyncio,subprocess,time,concurrent.futures
from pathlib import Path
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0,str(Path(__file__).resolve().parent))
from ai.voice_cloning import _run_capcut_tts,generate_tts_edge
BASE=Path(r"C:\tool v1\workspace\control\voice_checks")
BASE.mkdir(parents=True,exist_ok=True)
voices=json.loads((BASE.parent/"voice_catalog.json").read_text(encoding="utf-8"))
results=[]
def check(v):
    start=time.time();out=BASE/(v['id']+'.mp3')
    try:
        text="Xin chào, đây là giọng đọc thử. Hôm nay chúng ta cùng khám phá những món đồ thú vị."
        if v['source']=='capcut':_run_capcut_tts(text,str(out),v['param'],attempts=1,poll_interval=1)
        else:asyncio.run(generate_tts_edge(text,str(out),v['param']))
        p=subprocess.run(['ffprobe','-v','error','-show_entries','format=duration','-of','json',str(out)],capture_output=True,text=True,timeout=15)
        duration=float(json.loads(p.stdout)['format']['duration'])
        assert p.returncode==0 and duration>1
        return dict(id=v['id'],label=v['label'],ok=True,duration=duration,seconds=round(time.time()-start,1))
    except Exception as e:
        return dict(id=v['id'],label=v['label'],ok=False,error=type(e).__name__+': '+str(e)[:220],seconds=round(time.time()-start,1))
with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
    for result in pool.map(check,[v for v in voices if v['source']!='rvc']):
        results.append(result)
        (BASE/'results.json').write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps(result,ensure_ascii=False),flush=True)
