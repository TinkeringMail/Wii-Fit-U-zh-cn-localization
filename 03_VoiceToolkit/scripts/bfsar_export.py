#!/usr/bin/env python3
"""TuneBloom batch WAV exporter (BFSAR container).
Exports Wave Sound Sets to WAV, byte-identical to TuneBloom's own
WaveFile::writeWavFile (src/bfsar/WaveFile.cpp) + dsp/decode.cpp.
Also writes a per-set .txt manifest (exported WAV size, source encoded
data size, wave_id) for use when batch re-importing.

Usage: python bfsar_export.py <file.bfsar> [outdir] [--validate]
Stdlib only.
"""
import struct, sys, os

EB=">"; EL="<"
class R:
    def __init__(self,data):
        self.d=data
        self.e=EB if struct.unpack_from(">H",data,4)[0]==0xFEFF else EL
    def u16(self,o): return struct.unpack_from(self.e+"H",self.d,o)[0]
    def u32(self,o): return struct.unpack_from(self.e+"I",self.d,o)[0]
    def s32(self,o): return struct.unpack_from(self.e+"i",self.d,o)[0]
    def s16(self,o): return struct.unpack_from(self.e+"h",self.d,o)[0]
    def ref(self,o):   return (self.u16(o), self.u16(o+2), self.s32(o+4))      # typeId,pad,offset
    def refws(self,o): return (self.u16(o), self.u16(o+2), self.s32(o+4), self.u32(o+8))
    def cstr(self,o):
        e=self.d.index(b"\x00",o); return self.d[o:e].decode("latin-1","replace")

# element type ids (snd_ElementType.h)
ET_SAR_STR=0x2000; ET_SAR_IFO=0x2001; ET_SAR_FLE=0x2002
EI_WAVE=0x2202; EI_SEQ=0x2203; EI_WSNDGROUP=0x2205; EI_INTFILE=0x220C
ET_WARC_IFO=0x6800; ET_WARC_FLE=0x6801
ET_WFILE_IFO=0x7000; ET_WFILE_DAT=0x7001
ET_WSND_IFO=0x6800        # BFWSD INFO block (== 0x6800, same cat as wave archive)
WAVE_PCM8=0; WAVE_PCM16=1; WAVE_DSP=2
SPF=14

def dsp_decode(src,coef,yn1,yn2,samples):
    """Port of src/dsp/decode.cpp decode()."""
    out=[];i=0;rem=samples;h1=yn1;h2=yn2
    for _ in range((samples+SPF-1)//SPF):
        ps=src[i];i+=1
        pred=(ps>>4)&0xF; scale=1<<(ps&0xF)
        c1=coef[pred*2]; c2=coef[pred*2+1]
        n=min(SPF,rem)
        for s in range(n):
            b=src[i]
            smp=(b>>4)&0xF if s%2==0 else b&0xF
            if s%2==1: i+=1
            smp=smp-16 if smp>=8 else smp
            v=(((scale*smp)<<11)+1024+(c1*h1+c2*h2))>>11
            if v>32767:v=32767
            elif v<-32768:v=-32768
            h2=h1;h1=v;out.append(v)
        rem-=n
    return out

def parse_bfwav(r,base):
    nb=r.u16(base+0x10); ifo=dat=None; datSize=0
    for k in range(nb):
        tid,pad,off,size=r.refws(base+0x14+k*12)
        if tid==ET_WFILE_IFO: ifo=base+off
        elif tid==ET_WFILE_DAT: dat=base+off; datSize=size
    if ifo is None or dat is None: raise ValueError("BFWAV block missing")
    b=ifo+8
    enc=r.d[b]; sr=r.u32(b+4); loopEnd=r.u32(b+12)
    chnTab=b+20; nchn=r.u32(chnTab); datBody=dat+8
    channels=[]
    for c in range(nchn):
        tid,pad,off=r.ref(chnTab+4+c*8)
        chn=chnTab+off
        tid,pad,soff=r.ref(chn)          # referToSamples
        tid,pad,aoff=r.ref(chn+8)        # referToAdpcmInfo
        sa=datBody+soff
        ns=loopEnd if loopEnd>0 else 0
        if enc==WAVE_PCM8:
            ch=[r.d[sa+i]-128 for i in range(ns)]
        elif enc==WAVE_PCM16:
            ch=[r.s16(sa+i*2) for i in range(ns)]
        else:
            bl=(ns//SPF)*8; f=ns%SPF
            if f: bl+=((f+1)>>1)+1
            ada=chn+aoff
            coef=[r.s16(ada+i*2) for i in range(16)]
            # DspAdpcmParam: coef[8][2]@0, predScale@32, yn1@34, yn2@36
            ch=dsp_decode(r.d[sa:sa+bl],coef,r.s16(ada+34),r.s16(ada+36),ns)
        channels.append(ch)
    return {"enc":enc,"rate":sr,"channels":channels,"datSize":datSize}

def parse_fwar(r,base):
    nb=r.u16(base+0x10); ifo=fle=None
    for k in range(nb):
        tid,pad,off,size=r.refws(base+0x14+k*12)
        if tid==ET_WARC_IFO: ifo=base+off
        elif tid==ET_WARC_FLE: fle=base+off
    if ifo is None or fle is None: raise ValueError("FWAR block missing")
    tab=ifo+8; cnt=r.u32(tab); waves=[]
    for i in range(cnt):
        tid,pad,off,size=r.refws(tab+4+i*12)
        waves.append(fle+8+off)
    return waves

def wsnd_wave_id(r,base,dataIdx):
    """Map BFWSD wave-sound data index -> (waveArchiveId, waveIndex)."""
    nb=r.u16(base+0x10); info=None
    for k in range(nb):
        tid,pad,off,size=r.refws(base+0x14+k*12)
        if tid==ET_WSND_IFO: info=base+off
    if info is None: raise ValueError("BFWSD INFO missing")
    body=info+8
    tid,pad,offW=r.ref(body)        # toWaveIdTable
    tid,pad,offD=r.ref(body+8)      # toWaveSoundDataReferenceTable
    waveIdTab=body+offW; dataTab=body+offD
    if dataIdx>=r.u32(dataTab): return None
    tid,pad,off=r.ref(dataTab+4+dataIdx*8)
    meta=dataTab+off
    tid,pad,offN=r.ref(meta+16)     # toNoteInfoReferenceTable
    noteTab=meta+offN
    if r.u32(noteTab)==0: return None
    tid,pad,noff=r.ref(noteTab+4)   # note 0
    wIdx=r.u32(noteTab+noff)        # NoteInfo.waveIdTableIndex
    wid=waveIdTab+4+wIdx*8
    return (r.u32(wid), r.u32(wid+4))

def write_wav(path,enc,rate,channels):
    nc=len(channels); bits=8 if enc==WAVE_PCM8 else 16; ns=len(channels[0])
    le16=lambda x:struct.pack("<h",x); le32=lambda x:struct.pack("<I",x)
    data=bytearray()
    for i in range(ns):
        for c in range(nc):
            if enc==WAVE_PCM8: data.append((channels[c][i]+128)&0xFF)
            else: data+=le16(channels[c][i])
    with open(path,"wb") as f:
        f.write(b"RIFF"); f.write(le32(36+len(data))); f.write(b"WAVE")
        f.write(b"fmt "); f.write(le32(0x10)); f.write(struct.pack("<H",1))
        f.write(struct.pack("<H",nc)); f.write(le32(rate))
        f.write(le32(rate*nc*(bits//8))); f.write(struct.pack("<H",nc*(bits//8)))
        f.write(struct.pack("<H",bits))
        f.write(b"data"); f.write(le32(ns*nc*(bits//8)))
        f.write(bytes(data))

def load_bfsar(data):
    r=R(data)
    if data[:4]!=b"FSAR": raise ValueError("Not a BFSAR")
    s_off=i_off=f_off=None
    for k in range(3):
        tid,pad,off,size=r.refws(0x14+k*12)
        if tid==ET_SAR_STR: s_off=off
        elif tid==ET_SAR_IFO: i_off=off
        elif tid==ET_SAR_FLE: f_off=off
    if s_off is None or i_off is None or f_off is None: raise ValueError("blocks")
    sb=s_off+8
    tid,pad,stOff=r.ref(sb)                 # toSection[0] = StringTable
    stab=sb+stOff; stcnt=r.u32(stab)
    def get_str(sid):
        if sid==0xFFFFFFFF or sid>=stcnt: return None
        return r.cstr(stab + r.s32(stab+4+sid*12+4))
    ib=i_off+8
    tid,pad,sgOff=r.ref(ib+8);  sgTab=ib+sgOff;  sgcnt=r.u32(sgTab)
    tid,pad,fiOff=r.ref(ib+48); fiTab=ib+fiOff;  ficnt=r.u32(fiTab)
    tid,pad,waOff=r.ref(ib+24); waTab=ib+waOff;  wacnt=r.u32(waTab)
    tid,pad,sdOff=r.ref(ib);    sndTab=ib+sdOff; sndcnt=r.u32(sndTab)
    sets=[]
    for g in range(sgcnt):
        tid,pad,off=r.ref(sgTab+4+g*8); go=sgTab+off
        start=r.u32(go)&0xFFFFFF; end=r.u32(go+4)&0xFFFFFF
        dtid,dpad,doff=r.ref(go+16); isWave=(dtid==EI_WSNDGROUP)
        opt=r.u32(go+24); name=None
        if opt&1: name=get_str(r.u32(go+28))
        if not name: name=("WSDSET_%u"%g) if isWave else ("SEQSET_%u"%g)
        sets.append({"idx":g,"name":name,"wave":isWave,"start":start,"end":end})
    sounds=[]
    for si in range(sndcnt):
        tid,pad,off=r.ref(sndTab+4+si*8); so=sndTab+off
        fileId=r.u32(so)
        dtid,dpad,doff=r.ref(so+12)
        typ=("seq" if dtid==EI_SEQ else "wave" if dtid==EI_WAVE else "strm")
        wsIdx=0
        if dtid==EI_WAVE:
            wsIdx=r.u32(so+doff)            # SoundArchive::WaveSoundInfo.index
        opt=r.u32(so+20); nm=None
        if opt&1: nm=get_str(r.u32(so+24))
        if not nm: nm="SOUND_%u"%si
        sounds.append({"idx":si,"fileId":fileId,"type":typ,"name":nm,"waveIdx":wsIdx})
    files=[]
    for fi in range(ficnt):
        tid,pad,off=r.ref(fiTab+4+fi*8); fo=fiTab+off
        ltid,lpad,loff=r.ref(fo)
        if ltid==EI_INTFILE:
            off=r.s32(fo+loff+4); size=r.u32(fo+loff+8)
            files.append({"idx":fi,"internal":True,"abs":f_off+8+off,"size":size})
        else:
            files.append({"idx":fi,"internal":False})
    archives=[]
    for a in range(wacnt):
        tid,pad,off=r.ref(waTab+4+a*8); ao=waTab+off
        aid=r.u32(ao)
        img=files[aid]["abs"] if aid<len(files) and files[aid]["internal"] else None
        archives.append({"fileId":aid,"abs":img})
    return {"r":r,"sounds":sounds,"sets":sets,"files":files,"archives":archives}

def main():
    if len(sys.argv)<2:
        print("usage: python bfsar_export.py <file.bfsar> [outdir] [--validate]"); return
    data=open(sys.argv[1],"rb").read()
    b=load_bfsar(data)
    wsets=[s for s in b["sets"] if s["wave"]]
    ssets=[s for s in b["sets"] if not s["wave"]]
    print("Wave Sound Sets: %d | Seq Sound Sets: %d | wave sounds: %d"
          %(len(wsets),len(ssets),len([s for s in b["sounds"] if s["type"]=="wave"])))
    print("\n-- Wave Sound Sets --")
    for i,s in enumerate(wsets,1):
        print("  [%d] %-40s sounds %d..%d"%(i,s["name"],s["start"],s["end"]))
    print("\n-- Sequence Sound Sets (not WAV-exportable in TuneBloom) --")
    for i,s in enumerate(ssets,1):
        print("  [%d] %-40s sounds %d..%d"%(i,s["name"],s["start"],s["end"]))
    if "--validate" in sys.argv:
        print("\nvalidate OK"); return
    sel=input("\nSelect Wave Sound Sets (e.g. 1,2,3 / 1-5 / 'all'): ").strip()
    picked=[]
    if sel.lower()=="all":
        picked=list(range(1,len(wsets)+1))
    else:
        for p in sel.split(","):
            p=p.strip()
            if "-" in p:
                a,c=p.split("-"); picked+=list(range(int(a),int(c)+1))
            elif p: picked.append(int(p))
    outdir=sys.argv[2] if len(sys.argv)>2 else "export"
    os.makedirs(outdir,exist_ok=True)
    r=b["r"]
    aw=[parse_fwar(r,a["abs"]) if a["abs"] is not None else [] for a in b["archives"]]
    flat=[]
    for l in aw: flat+=l
    total_picked=len(picked)
    for pi,idx in enumerate(picked,1):
        if idx<1 or idx>len(wsets):
            print("skip: invalid selection %d"%idx); continue
        s=wsets[idx-1]
        folder=os.path.join(outdir,s["name"]); os.makedirs(folder,exist_ok=True)
        snds=[so for si,so in enumerate(b["sounds"])
              if s["start"]<=si<=s["end"] and so["type"]=="wave"]
        total=len(snds); done=0; fail=0
        sizes=[]   # (filename, wav_size, src_data_size, wave_id)
        for so in snds:
            ok=False
            f=b["files"][so["fileId"]] if so["fileId"]<len(b["files"]) else None
            if f and f["internal"]:
                try: wid=wsnd_wave_id(r,f["abs"],so["waveIdx"])
                except Exception: wid=None
                if wid:
                    waId,wIndex=wid; img=None
                    if waId==0:
                        if wIndex<len(flat): img=flat[wIndex]
                    elif (waId&0xFFFFFF)<len(aw) and wIndex<len(aw[waId&0xFFFFFF]):
                        img=aw[waId&0xFFFFFF][wIndex]
                    if img:
                        try:
                            wf=parse_bfwav(r,img)
                            outname=so["name"]+".wav"
                            path=os.path.join(folder,outname)
                            write_wav(path,wf["enc"],wf["rate"],wf["channels"])
                            sizes.append((outname, os.path.getsize(path),
                                          wf.get("datSize",0), "%d:%d"%(waId,wIndex)))
                            done+=1; ok=True
                        except Exception: pass
            if not ok: fail+=1
            print("\r[set %d/%d] %s: %d/%d exported (failed %d)"
                  %(pi,total_picked,s["name"],done,total,fail), end="", flush=True)
        if sizes:
            txt=os.path.join(folder, s["name"]+".txt")
            with open(txt,"w",encoding="utf-8") as f:
                f.write("# Exported wav sizes + source-encoded data sizes (bytes).\n")
                f.write("# wave_id = (waveArchiveId:waveIndex) in the FWAR.\n")
                f.write("file\tsize_bytes\tsrc_data_bytes\twave_id\n")
                for nm,sz,d,wi in sizes:
                    f.write("%s\t%d\t%d\t%s\n"%(nm,sz,d,wi))
                f.write("# TOTAL set export bytes: %d\n"%sum(sz for _,sz,_,_ in sizes))
        print("\nExported %d wave(s) from [%d] '%s' -> %s  (failed %d)"
              %(done,idx,s["name"],folder,fail))
    print("\nDone.")

if __name__=="__main__":
    main()