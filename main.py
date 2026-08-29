from __future__ import annotations
import hashlib, hmac, json, os, secrets, sqlite3, subprocess, time, uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Depends, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

BASE = Path(__file__).resolve().parent.parent
DATA = Path(os.environ.get("KEEPERCOACH_DATA_DIR", str(BASE / "data")))
UPLOADS = DATA / "uploads"
DB = DATA / "keepercoach.db"
STATIC = Path(__file__).resolve().parent / "static"
DATA.mkdir(exist_ok=True); UPLOADS.mkdir(exist_ok=True)

app = FastAPI(title="KeeperCoach MVP", version="1.0.0")

SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS users(
 id TEXT PRIMARY KEY, email TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
 role TEXT NOT NULL CHECK(role IN ('keeper','coach','parent','admin')),
 password_hash TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions(
 token TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS keepers(
 id TEXT PRIMARY KEY, owner_user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 name TEXT NOT NULL, dob TEXT, club TEXT, team TEXT, position TEXT DEFAULT 'Goalkeeper',
 height_cm INTEGER, dominant_foot TEXT, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS matches(
 id TEXT PRIMARY KEY, keeper_id TEXT NOT NULL REFERENCES keepers(id) ON DELETE CASCADE,
 opponent TEXT NOT NULL, match_date TEXT NOT NULL, result TEXT, minutes INTEGER DEFAULT 90,
 competition TEXT, video_path TEXT, video_name TEXT, video_duration REAL,
 status TEXT NOT NULL DEFAULT 'ready', created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events(
 id TEXT PRIMARY KEY, match_id TEXT NOT NULL REFERENCES matches(id) ON DELETE CASCADE,
 second REAL NOT NULL DEFAULT 0, event_type TEXT NOT NULL, outcome TEXT,
 technique INTEGER DEFAULT 5, decision_making INTEGER DEFAULT 5, positioning INTEGER DEFAULT 5,
 execution INTEGER DEFAULT 5, note TEXT, created_at TEXT NOT NULL
);
"""

def db():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    return con

def now(): return datetime.now(timezone.utc).isoformat()
def uid(): return uuid.uuid4().hex

def hash_password(password: str, salt: Optional[bytes]=None) -> str:
    salt = salt or secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac('sha256', password.encode(), salt, 180_000)
    return f"{salt.hex()}:{dk.hex()}"

def verify_password(password: str, encoded: str) -> bool:
    try:
        s, d = encoded.split(':',1)
        test = hash_password(password, bytes.fromhex(s)).split(':',1)[1]
        return hmac.compare_digest(test,d)
    except Exception: return False

def init_db():
    with db() as con:
        con.executescript(SCHEMA)
        row = con.execute("SELECT id FROM users WHERE email=?", ('demo@keepercoach.app',)).fetchone()
        if not row:
            user_id = uid(); keeper_id = uid(); match_id = uid()
            con.execute("INSERT INTO users VALUES(?,?,?,?,?,?)", (user_id,'demo@keepercoach.app','Demo Coach','coach',hash_password('keepercoach'),now()))
            con.execute("INSERT INTO keepers(id,owner_user_id,name,dob,club,team,position,height_cm,dominant_foot,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (keeper_id,user_id,'Zac Holston','2011-06-16','Blacktown City FC','U15 NPL','Goalkeeper',183,'Right',now()))
            con.execute("INSERT INTO matches(id,keeper_id,opponent,match_date,result,minutes,competition,status,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                        (match_id,keeper_id,'Sample Opponent','2026-08-23','2-1',90,'NPL NSW','ready',now()))
            samples=[(442,'Save','Saved',9,8,8,9,'Strong set position and clean hands.'),(796,'Distribution','Complete',7,7,7,8,'Good choice; first touch could open the angle sooner.'),(1921,'1v1','Saved',9,9,9,9,'Excellent patience and timing.'),(2902,'Goal conceded','Goal',7,8,7,7,'Limited chance; starting position was reasonable.'),(3363,'Sweeper','Won',8,9,8,8,'Positive decision to protect space behind the line.')]
            for sec,typ,out,t,d,p,e,n in samples:
                con.execute("INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?,?,?)",(uid(),match_id,sec,typ,out,t,d,p,e,n,now()))

init_db()

class Register(BaseModel):
    email:str; password:str; name:str; role:str='keeper'
class Login(BaseModel): email:str; password:str
class KeeperIn(BaseModel):
    name:str; dob:Optional[str]=None; club:Optional[str]=None; team:Optional[str]=None; height_cm:Optional[int]=None; dominant_foot:Optional[str]=None
class MatchIn(BaseModel):
    keeper_id:str; opponent:str; match_date:str; result:Optional[str]=None; minutes:int=90; competition:Optional[str]=None
class EventIn(BaseModel):
    second:float=0; event_type:str; outcome:Optional[str]=None; technique:int=5; decision_making:int=5; positioning:int=5; execution:int=5; note:Optional[str]=None

def get_user(authorization: Optional[str]=Header(default=None)):
    if not authorization or not authorization.lower().startswith('bearer '): raise HTTPException(401,'Sign in required')
    token=authorization.split(' ',1)[1]
    with db() as con:
        row=con.execute("SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token=?",(token,)).fetchone()
    if not row: raise HTTPException(401,'Session expired')
    return dict(row)

def keeper_owned(con, keeper_id, user_id):
    row=con.execute("SELECT * FROM keepers WHERE id=? AND owner_user_id=?",(keeper_id,user_id)).fetchone()
    if not row: raise HTTPException(404,'Keeper not found')
    return row

def match_owned(con, match_id, user_id):
    row=con.execute("SELECT m.* FROM matches m JOIN keepers k ON k.id=m.keeper_id WHERE m.id=? AND k.owner_user_id=?",(match_id,user_id)).fetchone()
    if not row: raise HTTPException(404,'Match not found')
    return row

@app.post('/api/register')
def register(x:Register):
    if len(x.password)<8: raise HTTPException(400,'Password must be at least 8 characters')
    if x.role not in {'keeper','coach','parent'}: raise HTTPException(400,'Invalid role')
    with db() as con:
        try:
            user_id=uid(); con.execute("INSERT INTO users VALUES(?,?,?,?,?,?)",(user_id,x.email.lower().strip(),x.name.strip(),x.role,hash_password(x.password),now()))
        except sqlite3.IntegrityError: raise HTTPException(409,'Email already registered')
        token=secrets.token_urlsafe(32); con.execute("INSERT INTO sessions VALUES(?,?,?)",(token,user_id,now()))
    return {'token':token}

@app.post('/api/login')
def login(x:Login):
    with db() as con:
        row=con.execute("SELECT * FROM users WHERE email=?",(x.email.lower().strip(),)).fetchone()
        if not row or not verify_password(x.password,row['password_hash']): raise HTTPException(401,'Incorrect email or password')
        token=secrets.token_urlsafe(32); con.execute("INSERT INTO sessions VALUES(?,?,?)",(token,row['id'],now()))
    return {'token':token}

@app.post('/api/logout')
def logout(authorization: Optional[str]=Header(default=None)):
    if authorization and authorization.lower().startswith('bearer '):
        with db() as con: con.execute("DELETE FROM sessions WHERE token=?",(authorization.split(' ',1)[1],))
    return {'ok':True}

@app.get('/api/me')
def me(user=Depends(get_user)):
    return {k:user[k] for k in ('id','email','name','role','created_at')}

@app.get('/api/keepers')
def keepers(user=Depends(get_user)):
    with db() as con: rows=con.execute("SELECT * FROM keepers WHERE owner_user_id=? ORDER BY created_at",(user['id'],)).fetchall()
    return [dict(r) for r in rows]

@app.post('/api/keepers')
def create_keeper(x:KeeperIn,user=Depends(get_user)):
    kid=uid()
    with db() as con:
        con.execute("INSERT INTO keepers(id,owner_user_id,name,dob,club,team,position,height_cm,dominant_foot,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (kid,user['id'],x.name,x.dob,x.club,x.team,'Goalkeeper',x.height_cm,x.dominant_foot,now()))
    return {'id':kid}

@app.get('/api/matches')
def matches(keeper_id:Optional[str]=None,user=Depends(get_user)):
    with db() as con:
        q="SELECT m.*,k.name keeper_name FROM matches m JOIN keepers k ON k.id=m.keeper_id WHERE k.owner_user_id=?"; args=[user['id']]
        if keeper_id: q+=' AND m.keeper_id=?'; args.append(keeper_id)
        q+=' ORDER BY match_date DESC, m.created_at DESC'
        rows=con.execute(q,args).fetchall()
    return [dict(r) for r in rows]

@app.post('/api/matches')
def create_match(x:MatchIn,user=Depends(get_user)):
    mid=uid()
    with db() as con:
        keeper_owned(con,x.keeper_id,user['id'])
        con.execute("INSERT INTO matches(id,keeper_id,opponent,match_date,result,minutes,competition,status,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (mid,x.keeper_id,x.opponent,x.match_date,x.result,x.minutes,x.competition,'ready',now()))
    return {'id':mid}

def video_duration(path:Path):
    try:
        p=subprocess.run(['ffprobe','-v','error','-show_entries','format=duration','-of','default=noprint_wrappers=1:nokey=1',str(path)],capture_output=True,text=True,timeout=15)
        return round(float(p.stdout.strip()),2) if p.returncode==0 and p.stdout.strip() else None
    except Exception: return None

@app.post('/api/matches/{match_id}/video')
async def upload_video(match_id:str,file:UploadFile=File(...),user=Depends(get_user)):
    allowed={'.mp4','.mov','.m4v','.webm'}; ext=Path(file.filename or '').suffix.lower()
    if ext not in allowed: raise HTTPException(400,'Upload MP4, MOV, M4V or WebM video')
    with db() as con: match_owned(con,match_id,user['id'])
    dest=UPLOADS/f"{match_id}{ext}"
    size=0
    with dest.open('wb') as out:
        while chunk:=await file.read(1024*1024):
            size+=len(chunk)
            max_upload_mb = int(os.environ.get('KEEPERCOACH_MAX_UPLOAD_MB', '500'))
            if size > max_upload_mb * 1024 * 1024:
                out.close(); dest.unlink(missing_ok=True); raise HTTPException(413, f'Video exceeds {max_upload_mb} MB upload limit')
            out.write(chunk)
    dur=video_duration(dest)
    with db() as con: con.execute("UPDATE matches SET video_path=?,video_name=?,video_duration=?,status='ready' WHERE id=?",(dest.name,file.filename,dur,match_id))
    return {'ok':True,'duration':dur,'name':file.filename}

@app.get('/api/matches/{match_id}/video')
def get_video(match_id:str,user=Depends(get_user)):
    with db() as con: m=match_owned(con,match_id,user['id'])
    if not m['video_path']: raise HTTPException(404,'No video uploaded')
    p=UPLOADS/m['video_path']
    if not p.exists(): raise HTTPException(404,'Video file missing')
    return FileResponse(p,filename=m['video_name'] or p.name)

@app.get('/api/matches/{match_id}/events')
def get_events(match_id:str,user=Depends(get_user)):
    with db() as con:
        match_owned(con,match_id,user['id']); rows=con.execute("SELECT * FROM events WHERE match_id=? ORDER BY second",(match_id,)).fetchall()
    return [dict(r) for r in rows]

@app.post('/api/matches/{match_id}/events')
def create_event(match_id:str,x:EventIn,user=Depends(get_user)):
    eid=uid()
    vals=[x.technique,x.decision_making,x.positioning,x.execution]
    if any(v<1 or v>10 for v in vals): raise HTTPException(400,'Scores must be 1-10')
    with db() as con:
        match_owned(con,match_id,user['id'])
        con.execute("INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?,?,?)",(eid,match_id,x.second,x.event_type,x.outcome,*vals,x.note,now()))
    return {'id':eid}

@app.delete('/api/events/{event_id}')
def delete_event(event_id:str,user=Depends(get_user)):
    with db() as con:
        row=con.execute("SELECT e.id FROM events e JOIN matches m ON m.id=e.match_id JOIN keepers k ON k.id=m.keeper_id WHERE e.id=? AND k.owner_user_id=?",(event_id,user['id'])).fetchone()
        if not row: raise HTTPException(404,'Event not found')
        con.execute("DELETE FROM events WHERE id=?",(event_id,))
    return {'ok':True}

def report_for(events):
    if not events:
        return {'overall':0,'categories':{},'strength':'Add events to generate a strength','focus':'Tag the match to identify a development focus','summary':'No tagged actions yet.','training':[]}
    types={}
    for e in events:
        score=(e['technique']+e['decision_making']+e['positioning']+e['execution'])/4*10
        types.setdefault(e['event_type'],[]).append(score)
    cats={k:round(sum(v)/len(v)) for k,v in types.items()}
    overall=round(sum(cats.values())/len(cats))
    strength=max(cats,key=cats.get); focus=min(cats,key=cats.get)
    tips={
      'Distribution':['First touch away from pressure','Driven passes into wide targets','Decision making under a pressing trigger'],
      'Cross':['Starting position on wide deliveries','High-ball timing and take-off','Traffic management and communication'],
      '1v1':['Delay and stay big','Approach speed and set timing','Block/spread technique'],
      'Save':['Set position before the strike','Handling and rebound control','Footwork into line of ball'],
      'Goal conceded':['Review starting position','Identify earlier visual cues','Reset and next-action routine'],
      'Sweeper':['Starting height relative to back line','Decision to hold or attack space','First action after regaining possession']
    }
    training=tips.get(focus,['Technique repetition','Decision-making scenarios','Pressure execution'])
    summary=f"{strength} was the strongest area in the tagged actions. The clearest development opportunity was {focus.lower()}. Build the next training block around that theme, then compare the next match report."
    return {'overall':overall,'categories':cats,'strength':strength,'focus':focus,'summary':summary,'training':training}

@app.get('/api/matches/{match_id}/report')
def report(match_id:str,user=Depends(get_user)):
    with db() as con:
        m=match_owned(con,match_id,user['id']); ev=con.execute("SELECT * FROM events WHERE match_id=? ORDER BY second",(match_id,)).fetchall()
    return {'match':dict(m),'report':report_for([dict(x) for x in ev]),'event_count':len(ev)}

@app.get('/api/progress/{keeper_id}')
def progress(keeper_id:str,user=Depends(get_user)):
    with db() as con:
        keeper_owned(con,keeper_id,user['id'])
        ms=con.execute("SELECT * FROM matches WHERE keeper_id=? ORDER BY match_date",(keeper_id,)).fetchall()
        out=[]
        for m in ms:
            ev=con.execute("SELECT * FROM events WHERE match_id=?",(m['id'],)).fetchall(); r=report_for([dict(x) for x in ev])
            out.append({'id':m['id'],'date':m['match_date'],'opponent':m['opponent'],'overall':r['overall'],'categories':r['categories']})
    return out

@app.get('/api/export')
def export(user=Depends(get_user)):
    with db() as con:
        ks=[dict(r) for r in con.execute("SELECT * FROM keepers WHERE owner_user_id=?",(user['id'],)).fetchall()]
        ids=[k['id'] for k in ks]
        ms=[]; es=[]
        for kid in ids:
            for m in con.execute("SELECT * FROM matches WHERE keeper_id=?",(kid,)).fetchall():
                md=dict(m); md.pop('video_path',None); ms.append(md)
                es += [dict(e) for e in con.execute("SELECT * FROM events WHERE match_id=?",(m['id'],)).fetchall()]
    return {'exported_at':now(),'user':{k:user[k] for k in ('id','email','name','role')},'keepers':ks,'matches':ms,'events':es}

@app.get('/api/health')
def health(): return {'ok':True,'version':'1.1.0','storage':str(DATA)}

app.mount('/static',StaticFiles(directory=STATIC),name='static')
@app.get('/')
def root(): return FileResponse(STATIC/'index.html')
