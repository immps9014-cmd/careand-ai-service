from faster_whisper import WhisperModel
import time, gc, math, glob, os
AUDIOS = sorted(glob.glob("/var/www/careand-backend/storage/app/voice-logs/1/*.webm"))
print(f"audio files: {len(AUDIOS)}")
for name, path in [("base","/root/careand-ai-service/models/base-ct2"),
                   ("small","/root/careand-ai-service/models/small-ct2")]:
    tload=time.time()
    m=WhisperModel(path, device="cpu", compute_type="int8")
    load_el=time.time()-tload
    mb=os.path.getsize(path+"/model.bin")/1e6
    print(f"\n##### MODEL={name}  load={load_el:.1f}s  size={mb:.0f}MB")
    for i,a in enumerate(AUDIOS):
        t=time.time()
        segs,info=m.transcribe(a, language="ko", vad_filter=True)
        sl=list(segs)
        el=time.time()-t
        text=" ".join(s.text.strip() for s in sl).strip()
        if sl:
            avg=sum(s.avg_logprob for s in sl)/len(sl)
            conf=round(max(0.0,min(math.exp(avg),1.0)),3)
        else:
            conf=0.0
        rtf=el/info.duration if info.duration else 0
        print(f"  [{name}] file{i+1} dur={info.duration:.1f}s  elapsed={el:.1f}s  RTF={rtf:.2f}x  conf={conf}")
        print(f"        TEXT: {text}")
    del m; gc.collect()
