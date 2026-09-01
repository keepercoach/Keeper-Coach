from __future__ import annotations
import base64, hashlib, hmac, json, logging, os, secrets, shutil, sqlite3, subprocess, tempfile, time, urllib.error, urllib.request, uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, BackgroundTasks, Depends, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
import boto3
import requests
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

BASE = Path(__file__).resolve().parent.parent
DATA = Path(os.environ.get("KEEPERCOACH_DATA_DIR", str(BASE / "data")))
UPLOADS = DATA / "uploads"
DB = DATA / "keepercoach.db"
STATIC = Path(__file__).resolve().parent 
DATA.mkdir(exist_ok=True); UPLOADS.mkdir(exist_ok=True)

app = FastAPI(title="KeeperCoach MVP", version="1.0.0")
logger = logging.getLogger("keepercoach.ai")

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
CREATE TABLE IF NOT EXISTS analysis_jobs(
 id TEXT PRIMARY KEY, match_id TEXT NOT NULL REFERENCES matches(id) ON DELETE CASCADE,
 status TEXT NOT NULL, progress INTEGER NOT NULL DEFAULT 0, message TEXT,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ai_suggestions(
 id TEXT PRIMARY KEY, match_id TEXT NOT NULL REFERENCES matches(id) ON DELETE CASCADE,
 second REAL NOT NULL, event_type TEXT NOT NULL, outcome TEXT, confidence REAL NOT NULL,
 technique INTEGER DEFAULT 5, decision_making INTEGER DEFAULT 5,
 positioning INTEGER DEFAULT 5, execution INTEGER DEFAULT 5,
 note TEXT, status TEXT NOT NULL DEFAULT 'pending', created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS match_ai_config(
 match_id TEXT PRIMARY KEY REFERENCES matches(id) ON DELETE CASCADE,
 goalkeeper_description TEXT NOT NULL, updated_at TEXT NOT NULL
);
"""

def db():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    return con

def now(): return datetime.now(timezone.utc).isoformat()
def uid(): return uuid.uuid4().hex

def bucket_configured():
    return all(os.environ.get(name) for name in ('AWS_ACCESS_KEY_ID','AWS_SECRET_ACCESS_KEY','AWS_ENDPOINT_URL','AWS_S3_BUCKET_NAME'))

def s3_client():
    if not bucket_configured(): raise RuntimeError('Video storage bucket is not configured')
    return boto3.client('s3',endpoint_url=os.environ['AWS_ENDPOINT_URL'],region_name=os.environ.get('AWS_DEFAULT_REGION','auto'))

def s3_key(match_id, ext): return f"matches/{match_id}/footage{ext}"
def is_s3_path(path): return bool(path and path.startswith('s3:'))
def object_key(path): return path[3:] if is_s3_path(path) else None

def video_source(path):
    if is_s3_path(path):
        return s3_client().generate_presigned_url('get_object',Params={'Bucket':os.environ['AWS_S3_BUCKET_NAME'],'Key':object_key(path)},ExpiresIn=3600)
    return str(UPLOADS/Path(path).name)

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

def expire_interrupted_analysis_jobs():
    """Background tasks do not survive a deployment or container restart."""
    with db() as con:
        con.execute(
            "UPDATE analysis_jobs SET status='failed',progress=0,message=?,updated_at=? "
            "WHERE status IN ('queued','running')",
            ('The scan was interrupted by an app restart. Please start it again.',now()))

expire_interrupted_analysis_jobs()

class Register(BaseModel):
    email:str; password:str; name:str; role:str='keeper'
class Login(BaseModel): email:str; password:str
class KeeperIn(BaseModel):
    name:str; dob:Optional[str]=None; club:Optional[str]=None; team:Optional[str]=None; height_cm:Optional[int]=None; dominant_foot:Optional[str]=None
class MatchIn(BaseModel):
    keeper_id:str; opponent:str; match_date:str; result:Optional[str]=None; minutes:int=90; competition:Optional[str]=None
class VideoUploadStart(BaseModel):
    filename:str; size:int
class AIAnalysisStart(BaseModel):
    goalkeeper_description:str
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

@app.delete('/api/matches/{match_id}')
def delete_match(match_id:str,user=Depends(get_user)):
    video_file = None
    video_key = None
    with db() as con:
        match = match_owned(con,match_id,user['id'])
        if match['video_path']:
            if is_s3_path(match['video_path']): video_key=object_key(match['video_path'])
            else:
                candidate = (UPLOADS / Path(match['video_path']).name).resolve()
                if candidate.parent == UPLOADS.resolve(): video_file = candidate
        # Events are removed by the foreign-key cascade in the same transaction.
        con.execute("DELETE FROM matches WHERE id=?",(match_id,))
    if video_file:
        try:
            video_file.unlink(missing_ok=True)
        except OSError:
            # The match is already safely deleted; a storage cleanup failure should
            # not make the client retry a destructive operation.
            pass
    if video_key:
        try: s3_client().delete_object(Bucket=os.environ['AWS_S3_BUCKET_NAME'],Key=video_key)
        except Exception: pass
    return {'ok':True}

def video_duration(path:Path):
    try:
        p=subprocess.run(['ffprobe','-v','error','-show_entries','format=duration','-of','default=noprint_wrappers=1:nokey=1',str(path)],capture_output=True,text=True,timeout=15)
        return round(float(p.stdout.strip()),2) if p.returncode==0 and p.stdout.strip() else None
    except Exception: return None

def upload_paths(match_id:str,upload_id:str):
    safe_upload_id=''.join(c for c in upload_id if c.isalnum())
    if not safe_upload_id or safe_upload_id != upload_id: raise HTTPException(400,'Invalid upload')
    return UPLOADS/f"{match_id}.{upload_id}.part",UPLOADS/f"{match_id}.{upload_id}.json"

@app.post('/api/matches/{match_id}/video/start')
def start_video_upload(match_id:str,x:VideoUploadStart,user=Depends(get_user)):
    allowed={'.mp4','.mov','.m4v','.webm'}; ext=Path(x.filename or '').suffix.lower()
    if ext not in allowed: raise HTTPException(400,'Upload MP4, MOV, M4V or WebM video')
    max_bytes=int(os.environ.get('KEEPERCOACH_MAX_UPLOAD_MB','5120'))*1024*1024
    if x.size<=0 or x.size>max_bytes: raise HTTPException(413,f'Video exceeds the {max_bytes//(1024*1024)} MB upload limit')
    with db() as con: match_owned(con,match_id,user['id'])
    upload_id=uid(); part,meta=upload_paths(match_id,upload_id)
    details={'filename':x.filename,'size':x.size,'ext':ext,'uploaded':0,'parts':[]}
    if bucket_configured():
        key=s3_key(match_id,ext)
        result=s3_client().create_multipart_upload(Bucket=os.environ['AWS_S3_BUCKET_NAME'],Key=key,ContentType='video/mp4')
        details.update({'storage':'s3','key':key,'s3_upload_id':result['UploadId']})
    else:
        part.write_bytes(b''); details['storage']='local'
    meta.write_text(json.dumps(details),encoding='utf-8')
    return {'upload_id':upload_id,'uploaded':0}

@app.post('/api/matches/{match_id}/video/chunk/{upload_id}')
async def upload_video_chunk(match_id:str,upload_id:str,request:Request,offset:int,user=Depends(get_user)):
    with db() as con: match_owned(con,match_id,user['id'])
    part,meta_path=upload_paths(match_id,upload_id)
    if not meta_path.exists(): raise HTTPException(404,'Upload session not found')
    meta=json.loads(meta_path.read_text(encoding='utf-8'))
    current=int(meta.get('uploaded',0)) if meta.get('storage')=='s3' else part.stat().st_size
    if offset!=current: raise HTTPException(409,detail={'message':'Upload offset changed','uploaded':current})
    body=await request.body()
    if current+len(body)>meta['size']: raise HTTPException(413,'Upload exceeds expected file size')
    if meta.get('storage')=='s3':
        part_number=len(meta['parts'])+1
        result=s3_client().upload_part(Bucket=os.environ['AWS_S3_BUCKET_NAME'],Key=meta['key'],UploadId=meta['s3_upload_id'],PartNumber=part_number,Body=body)
        meta['parts'].append({'ETag':result['ETag'],'PartNumber':part_number}); current+=len(body); meta['uploaded']=current
        meta_path.write_text(json.dumps(meta),encoding='utf-8')
    else:
        with part.open('ab') as out: out.write(body)
        current+=len(body)
    return {'uploaded':current}

@app.post('/api/matches/{match_id}/video/complete/{upload_id}')
def complete_video_upload(match_id:str,upload_id:str,user=Depends(get_user)):
    with db() as con: match=match_owned(con,match_id,user['id'])
    part,meta_path=upload_paths(match_id,upload_id)
    if not meta_path.exists(): raise HTTPException(404,'Upload session not found')
    meta=json.loads(meta_path.read_text(encoding='utf-8'))
    uploaded=int(meta.get('uploaded',0)) if meta.get('storage')=='s3' else part.stat().st_size
    if uploaded!=meta['size']: raise HTTPException(400,'Upload is incomplete')
    if meta.get('storage')=='s3':
        s3_client().complete_multipart_upload(Bucket=os.environ['AWS_S3_BUCKET_NAME'],Key=meta['key'],UploadId=meta['s3_upload_id'],MultipartUpload={'Parts':meta['parts']})
        meta_path.unlink(missing_ok=True)
        stored_path='s3:'+meta['key']; source=video_source(stored_path)
        dur=video_duration(source)
        if is_s3_path(match['video_path']) and object_key(match['video_path'])!=meta['key']:
            s3_client().delete_object(Bucket=os.environ['AWS_S3_BUCKET_NAME'],Key=object_key(match['video_path']))
        with db() as con:
            match_owned(con,match_id,user['id'])
            con.execute("UPDATE matches SET video_path=?,video_name=?,video_duration=?,status='ready' WHERE id=?",(stored_path,meta['filename'],dur,match_id))
        return {'ok':True,'duration':dur,'name':meta['filename']}
    dest=UPLOADS/f"{match_id}{meta['ext']}"
    old_path=(UPLOADS/Path(match['video_path']).name) if match['video_path'] else None
    part.replace(dest); meta_path.unlink(missing_ok=True)
    if old_path and old_path!=dest: old_path.unlink(missing_ok=True)
    dur=video_duration(dest)
    with db() as con:
        match_owned(con,match_id,user['id'])
        con.execute("UPDATE matches SET video_path=?,video_name=?,video_duration=?,status='ready' WHERE id=?",(dest.name,meta['filename'],dur,match_id))
    return {'ok':True,'duration':dur,'name':meta['filename']}

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
            max_upload_mb = int(os.environ.get('KEEPERCOACH_MAX_UPLOAD_MB', '5120'))
            if size > max_upload_mb * 1024 * 1024:
                out.close(); dest.unlink(missing_ok=True); raise HTTPException(413, f'Video exceeds {max_upload_mb} MB upload limit')
            out.write(chunk)
    dur=video_duration(dest)
    with db() as con: con.execute("UPDATE matches SET video_path=?,video_name=?,video_duration=?,status='ready' WHERE id=?",(dest.name,file.filename,dur,match_id))
    return {'ok':True,'duration':dur,'name':file.filename}

@app.get('/api/matches/{match_id}/video')
def get_video(match_id:str,token:Optional[str]=None,authorization:Optional[str]=Header(default=None)):
    user=get_user(f"Bearer {token}" if token else authorization)
    with db() as con: m=match_owned(con,match_id,user['id'])
    if not m['video_path']: raise HTTPException(404,'No video uploaded')
    if is_s3_path(m['video_path']):
        return RedirectResponse(video_source(m['video_path']),status_code=307)
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

def update_analysis_job(job_id,status,progress,message):
    with db() as con:
        con.execute("UPDATE analysis_jobs SET status=?,progress=?,message=?,updated_at=? WHERE id=?",(status,progress,message,now(),job_id))

def openai_vision_json(content,schema,name):
    api_key=os.environ.get('OPENAI_API_KEY')
    if not api_key: raise RuntimeError('OpenAI API key is not configured')
    payload={'model':os.environ.get('OPENAI_VISION_MODEL','gpt-5.6-sol'),'input':[{'role':'user','content':content}],
             'text':{'format':{'type':'json_schema','name':name,'strict':True,'schema':schema}}}
    req=urllib.request.Request('https://api.openai.com/v1/responses',data=json.dumps(payload).encode(),headers={
        'Authorization':f'Bearer {api_key}','Content-Type':'application/json'})
    try:
        with urllib.request.urlopen(req,timeout=120) as response: result=json.loads(response.read())
    except urllib.error.HTTPError as exc:
        request_id=exc.headers.get('x-request-id','unknown')
        try:
            body=json.loads(exc.read().decode('utf-8','replace'))
            detail=body.get('error',{})
            error_code=detail.get('code') or detail.get('type') or 'api_error'
            error_message=detail.get('message') or 'OpenAI rejected the request'
        except Exception:
            error_code='api_error'; error_message='OpenAI rejected the request'
        raise RuntimeError(f'OpenAI API error {exc.code} ({error_code}): {error_message}; request_id={request_id}') from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f'Could not connect to OpenAI: {exc.reason}') from exc
    text=''
    for item in result.get('output',[]):
        if item.get('type')=='message':
            for part in item.get('content',[]):
                if part.get('type')=='output_text': text+=part.get('text','')
    return json.loads(text or '{"events":[]}')

def image_part(path,detail='low'):
    encoded=base64.b64encode(path.read_bytes()).decode()
    return {'type':'input_image','image_url':f'data:image/jpeg;base64,{encoded}','detail':detail}

def goalkeeper_frame_filter(description,second,duration,width):
    """Enlarge the described goal side while retaining enough pitch for action context."""
    words=description.lower()
    first_right='first half' in words and 'right' in words.split('second half')[0]
    second_text=words.split('second half',1)[1] if 'second half' in words else ''
    second_left='left' in second_text
    first_left='first half' in words and 'left' in words.split('second half')[0]
    second_right='right' in second_text
    side=('right' if first_right else 'left' if first_left else None) if second<float(duration)/2 else ('left' if second_left else 'right' if second_right else None)
    if side=='right': return f'crop=iw*0.70:ih:iw*0.30:0,scale={width}:-2'
    if side=='left': return f'crop=iw*0.70:ih:0:0,scale={width}:-2'
    return f'scale={width}:-2'

def openai_candidate_batch(frames,goalkeeper_description):
    content=[{'type':'input_text','text':(
        'This is a coarse search through a football match. Find frames that may show the TARGET goalkeeper '
        'or play occurring in the target goalkeeper’s defensive third. Include the frame whenever the target is '
        'visible, the ball is near their penalty area, they have the ball, or an attack is developing toward them. '
        'Possible later actions include shot/save, 1v1, cross claim/punch, distribution, sweeper action, or goal conceded. '
        'This is only a discovery pass, so strongly favour recall and let the later temporal review reject routine play. '
        f'TARGET goalkeeper: {goalkeeper_description}. Each image is preceded by its exact frame_index and time. '
        'Do not confuse the target with the other goalkeeper or outfield players. Return every plausible candidate.'
    )}]
    for index,(second,path) in enumerate(frames):
        content.append({'type':'input_text','text':f'frame_index={index}, match_time={second:.1f}s'})
        content.append(image_part(path,'high'))
    schema={'type':'object','properties':{'candidates':{'type':'array','items':{'type':'object','properties':{
        'frame_index':{'type':'integer','minimum':0},'confidence':{'type':'number','minimum':0,'maximum':1},
        'reason':{'type':'string'}},'required':['frame_index','confidence','reason'],'additionalProperties':False}}},
        'required':['candidates'],'additionalProperties':False}
    return openai_vision_json(content,schema,'goalkeeper_candidates')

def openai_sequence_batch(sequences,goalkeeper_description):
    content=[{'type':'input_text','text':(
        'Act as a careful goalkeeper video analyst. Each numbered moment contains nine chronological frames, '
        'one second apart from four seconds before to four seconds after the centre time. Use the full sequence '
        'to identify the setup, goalkeeper action, and outcome—not a single still. '
        'whether the TARGET goalkeeper performed a real event. Return at most one event per moment. '
        f'TARGET goalkeeper: {goalkeeper_description}. Ignore every other player and the opposite goalkeeper. '
        'Only report Save, 1v1, Cross, Distribution, Sweeper, or Goal conceded. The outcome and note must say '
        'Provide visible_evidence stating what changes across the frames and a separate coaching_point naming a '
        'specific technical detail (set shape, footwork, handling, body line, decision timing, recovery, or distribution). '
        'Do not use generic wording such as good effort, solid moment, or could improve. Scores are provisional '
        '1-10 assessments supported by the visible sequence. Omit moments where identity, action, or outcome is unclear.'
    )}]
    for moment_index,(second,paths) in enumerate(sequences):
        content.append({'type':'input_text','text':f'moment_index={moment_index}, centre_time={second:.1f}s; frames begin now in chronological order'})
        for path in paths: content.append(image_part(path,'high'))
    event={'type':'object','properties':{
        'moment_index':{'type':'integer','minimum':0},
        'event_type':{'type':'string','enum':['Save','1v1','Cross','Distribution','Sweeper','Goal conceded']},
        'outcome':{'type':'string'},'confidence':{'type':'number','minimum':0,'maximum':1},
        'technique':{'type':'integer','minimum':1,'maximum':10},'decision_making':{'type':'integer','minimum':1,'maximum':10},
        'positioning':{'type':'integer','minimum':1,'maximum':10},'execution':{'type':'integer','minimum':1,'maximum':10},
        'visible_evidence':{'type':'string'},'coaching_point':{'type':'string'}},
        'required':['moment_index','event_type','outcome','confidence','technique','decision_making','positioning','execution','visible_evidence','coaching_point'],
        'additionalProperties':False}
    schema={'type':'object','properties':{'events':{'type':'array','items':event}},'required':['events'],'additionalProperties':False}
    return openai_vision_json(content,schema,'goalkeeper_events')

def video_mime(filename):
    return {'.mov':'video/mov','.webm':'video/webm','.avi':'video/avi','.mpeg':'video/mpeg','.mpg':'video/mpeg'}.get(Path(filename or '').suffix.lower(),'video/mp4')

def parse_match_time(value):
    if isinstance(value,(int,float)): return float(value)
    parts=str(value or '').strip().split(':')
    try:
        total=0.0
        for part in parts: total=total*60+float(part)
        return total
    except (TypeError,ValueError): return -1

def gemini_video_analysis(match,goalkeeper_description,job_id):
    api_key=os.environ.get('GEMINI_API_KEY')
    if not api_key: raise RuntimeError('Gemini API key is not configured')
    mime=video_mime(match['video_name']); uploaded_name=None; analysis_temp=None
    if is_s3_path(match['video_path']):
        obj=s3_client().get_object(Bucket=os.environ['AWS_S3_BUCKET_NAME'],Key=object_key(match['video_path']))
        stream=obj['Body']; size=int(obj['ContentLength'])
    else:
        path=UPLOADS/Path(match['video_path']).name; stream=path.open('rb'); size=path.stat().st_size
    try:
        # Gemini currently rejects media above 2 GiB. Create a compact analysis
        # proxy while preserving the original match footage in object storage.
        if size>=1_900_000_000:
            try: stream.close()
            except Exception: pass
            analysis_temp=Path(tempfile.mkdtemp(prefix='keepercoach-gemini-'))
            proxy=analysis_temp/'analysis-proxy.mp4'; source=video_source(match['video_path'])
            update_analysis_job(job_id,'running',3,'Preparing a smaller video copy for analysis')
            result=subprocess.run(['ffmpeg','-loglevel','error','-i',source,'-vf','scale=1280:-2',
                '-c:v','libx264','-preset','veryfast','-b:v','1100k','-maxrate','1400k','-bufsize','2800k',
                '-c:a','aac','-b:a','64k','-movflags','+faststart','-y',str(proxy)],capture_output=True,timeout=10800)
            if result.returncode!=0 or not proxy.exists():
                detail=result.stderr.decode('utf-8','replace')[-500:]
                raise RuntimeError(f'Could not prepare the Gemini analysis copy: {detail}')
            stream=proxy.open('rb'); size=proxy.stat().st_size; mime='video/mp4'
            if size>=2_000_000_000: raise RuntimeError('The compressed analysis copy is still larger than Gemini permits')
        start=requests.post('https://generativelanguage.googleapis.com/upload/v1beta/files',headers={
            'x-goog-api-key':api_key,'X-Goog-Upload-Protocol':'resumable','X-Goog-Upload-Command':'start',
            'X-Goog-Upload-Header-Content-Length':str(size),'X-Goog-Upload-Header-Content-Type':mime,
            'Content-Type':'application/json'},json={'file':{'display_name':match['video_name'] or 'Keeper Coach match'}},timeout=60)
        start.raise_for_status(); upload_url=start.headers.get('X-Goog-Upload-URL') or start.headers.get('x-goog-upload-url')
        if not upload_url: raise RuntimeError('Gemini did not create a video upload session')
        offset=0; result=None; chunk_size=8*1024*1024
        while offset<size:
            chunk=stream.read(min(chunk_size,size-offset))
            if not chunk: raise RuntimeError('The stored match video ended before upload completed')
            final=offset+len(chunk)>=size
            result=requests.post(upload_url,data=chunk,headers={'Content-Length':str(len(chunk)),
                'X-Goog-Upload-Offset':str(offset),'X-Goog-Upload-Command':'upload, finalize' if final else 'upload'},timeout=180)
            result.raise_for_status(); offset+=len(chunk)
            update_analysis_job(job_id,'running',min(40,5+round(offset/size*35)),'Sending the match to the video analysis engine')
        info=result.json().get('file',{}); uploaded_name=info.get('name'); uri=info.get('uri')
        if not uploaded_name or not uri: raise RuntimeError('Gemini video upload did not return a usable file')
        for _ in range(180):
            status=requests.get(f'https://generativelanguage.googleapis.com/v1beta/{uploaded_name}',headers={'x-goog-api-key':api_key},timeout=30)
            status.raise_for_status(); file_info=status.json(); state=file_info.get('state')
            if state=='ACTIVE': break
            if state=='FAILED': raise RuntimeError('Gemini could not process the uploaded match video')
            update_analysis_job(job_id,'running',45,'Preparing the full video for analysis'); time.sleep(5)
        else: raise RuntimeError('Gemini video processing timed out')
        prompt=(
            'Analyse this complete football match as a qualified goalkeeper coach. The only TARGET goalkeeper is: '
            f'{goalkeeper_description}. Track only that goalkeeper and ignore the opposite goalkeeper. Find every clearly '
            'visible Save, 1v1, Cross, Distribution, Sweeper action, and Goal conceded. Use the video sequence and audio, '
            'not isolated frames. Return ONLY valid JSON with this shape: {"events":[{"timestamp":"MM:SS",'
            '"event_type":"Save|1v1|Cross|Distribution|Sweeper|Goal conceded","outcome":"specific visible result",'
            '"confidence":0.0,"technique":1,"decision_making":1,"positioning":1,"execution":1,'
            '"visible_evidence":"what happens before, during and after the action",'
            '"coaching_point":"one concrete technical observation"}]}. Include all genuine involvements, but omit routine '
            'standing and anything where identity or outcome is unclear. Never use vague phrases such as good effort, solid '
            'moment, or could improve. Scores must be integers from 1 to 10.'
        )
        update_analysis_job(job_id,'running',55,'Watching the complete match and locating goalkeeper actions')
        response=None
        for attempt in range(1,5):
            response=requests.post('https://generativelanguage.googleapis.com/v1beta/interactions',headers={
                'x-goog-api-key':api_key,'Content-Type':'application/json'},json={'model':os.environ.get('GEMINI_VIDEO_MODEL','gemini-3.7-flash'),
                'input':[{'type':'video','uri':uri,'mime_type':mime},{'type':'text','text':prompt}]},timeout=900)
            if response.status_code not in (429,500,502,503,504): break
            if attempt==4: break
            wait_seconds=attempt*30
            update_analysis_job(job_id,'running',55,
                f'Video analysis service is busy — retrying automatically ({attempt}/3)')
            time.sleep(wait_seconds)
        response.raise_for_status(); payload=response.json()
        texts=[]
        def collect(value):
            if isinstance(value,dict):
                for key,item in value.items():
                    if key=='text' and isinstance(item,str): texts.append(item)
                    else: collect(item)
            elif isinstance(value,list):
                for item in value: collect(item)
        collect(payload); raw='\n'.join(texts); begin=raw.find('{'); end=raw.rfind('}')
        if begin<0 or end<=begin: raise RuntimeError('Gemini returned no structured goalkeeper analysis')
        return json.loads(raw[begin:end+1])
    except requests.HTTPError as exc:
        try: detail=exc.response.json().get('error',{}).get('message')
        except Exception: detail=None
        raise RuntimeError(f'Gemini API error {exc.response.status_code}: {detail or "request failed"}') from exc
    finally:
        try: stream.close()
        except Exception: pass
        if analysis_temp: shutil.rmtree(analysis_temp,ignore_errors=True)
        if uploaded_name:
            try: requests.delete(f'https://generativelanguage.googleapis.com/v1beta/{uploaded_name}',headers={'x-goog-api-key':api_key},timeout=30)
            except Exception: pass

def run_ai_analysis(job_id,match_id):
    try:
        suggestion_count=0
        update_analysis_job(job_id,'running',1,'Preparing match footage')
        with db() as con:
            match=con.execute("SELECT * FROM matches WHERE id=?",(match_id,)).fetchone()
            config=con.execute("SELECT * FROM match_ai_config WHERE match_id=?",(match_id,)).fetchone()
        if not match or not match['video_path']: raise RuntimeError('No uploaded footage found')
        if not config: raise RuntimeError('Target goalkeeper description is missing')
        source=video_source(match['video_path'])
        if not is_s3_path(match['video_path']) and not Path(source).exists(): raise RuntimeError('Uploaded footage file is missing')
        duration=match['video_duration'] or video_duration(source)
        if not duration: raise RuntimeError('Could not determine video duration')
        with db() as con: con.execute("DELETE FROM ai_suggestions WHERE match_id=? AND status='pending'",(match_id,))
        if os.environ.get('AI_ANALYSIS_PROVIDER','gemini').lower()=='gemini':
            proposals=gemini_video_analysis(match,config['goalkeeper_description'],job_id)
            with db() as con:
                for proposal in proposals.get('events',[]):
                    second=parse_match_time(proposal.get('timestamp'))
                    confidence=float(proposal.get('confidence',0))
                    event_type=proposal.get('event_type')
                    evidence=str(proposal.get('visible_evidence') or '').strip()
                    coaching=str(proposal.get('coaching_point') or '').strip()
                    if second<0 or second>float(duration) or confidence<0.45 or event_type not in ('Save','1v1','Cross','Distribution','Sweeper','Goal conceded'): continue
                    score=lambda key:max(1,min(10,int(proposal.get(key,5))))
                    con.execute("INSERT INTO ai_suggestions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",(
                        uid(),match_id,second,event_type,str(proposal.get('outcome') or '').strip(),confidence,
                        score('technique'),score('decision_making'),score('positioning'),score('execution'),
                        f'{evidence} Coaching: {coaching}','pending',now()))
                    suggestion_count+=1
            message=(f'{suggestion_count} video-based AI suggestions ready for coach review' if suggestion_count else
                     'Video scan complete — no confident goalkeeper events found')
            update_analysis_job(job_id,'complete',100,message)
            return
        # A sparse 60-frame scan misses most goalkeeper actions. Search up to 450
        # frames first, then spend high-detail vision only on short candidate bursts.
        interval=max(6.0,float(duration)/600.0)
        timestamps=[min(float(duration)-0.1,i*interval+interval/2) for i in range(max(1,int(float(duration)/interval)))]
        timestamps=timestamps[:600]
        with tempfile.TemporaryDirectory() as temp:
            temp_path=Path(temp); frames=[]
            for i,second in enumerate(timestamps):
                frame=temp_path/f'frame-{i:03}.jpg'
                vf=goalkeeper_frame_filter(config['goalkeeper_description'],second,duration,1280)
                result=subprocess.run(['ffmpeg','-loglevel','error','-ss',str(second),'-i',source,'-frames:v','1','-vf',vf,'-q:v','3','-y',str(frame)],capture_output=True,timeout=45)
                if result.returncode==0 and frame.exists(): frames.append((second,frame))
                update_analysis_job(job_id,'running',min(25,3+round((i+1)/len(timestamps)*22)),'Sampling the full match at close intervals')
            if not frames: raise RuntimeError('No frames could be extracted from the footage')
            candidates=[]; batch_size=8
            for start in range(0,len(frames),batch_size):
                batch=frames[start:start+batch_size]; found=openai_candidate_batch(batch,config['goalkeeper_description'])
                for candidate in found.get('candidates',[]):
                    index=candidate.get('frame_index',-1)
                    if 0<=index<len(batch) and float(candidate.get('confidence',0))>=0.2:
                        candidates.append(batch[index][0])
                progress=25+round(min(len(frames),start+len(batch))/len(frames)*35)
                update_analysis_job(job_id,'running',progress,'Finding likely goalkeeper involvement')
            # Merge neighbouring hits into one moment so the same action is not returned repeatedly.
            moments=[]
            for second in sorted(candidates):
                if not moments or second-moments[-1]>16: moments.append(second)
                else: moments[-1]=(moments[-1]+second)/2
            if not moments:
                # Never let an over-cautious discovery response suppress the
                # temporal review completely. Review an even spread as fallback.
                stride=max(1,len(frames)//60)
                moments=[second for second,_ in frames[::stride]][:60]
            moments=moments[:90]
            sequences=[]
            for moment_index,second in enumerate(moments):
                paths=[]
                for frame_index,offset in enumerate((-4,-3,-2,-1,0,1,2,3,4)):
                    at=max(0.0,min(float(duration)-0.1,second+offset)); frame=temp_path/f'moment-{moment_index:03}-{frame_index}.jpg'
                    vf=goalkeeper_frame_filter(config['goalkeeper_description'],at,duration,1280)
                    result=subprocess.run(['ffmpeg','-loglevel','error','-ss',str(at),'-i',source,'-frames:v','1','-vf',vf,'-q:v','3','-y',str(frame)],capture_output=True,timeout=45)
                    if result.returncode==0 and frame.exists(): paths.append(frame)
                if len(paths)==9: sequences.append((second,paths))
                update_analysis_job(job_id,'running',60+round((moment_index+1)/max(1,len(moments))*10),'Building before-and-after action sequences')
            for start in range(0,len(sequences),2):
                batch=sequences[start:start+2]; proposals=openai_sequence_batch(batch,config['goalkeeper_description'])
                with db() as con:
                    for proposal in proposals.get('events',[]):
                        index=proposal.get('moment_index',-1); confidence=float(proposal.get('confidence',0))
                        evidence=str(proposal.get('visible_evidence') or '').strip()
                        coaching=str(proposal.get('coaching_point') or '').strip()
                        note=f'{evidence} Coaching: {coaching}'
                        outcome=str(proposal.get('outcome') or '').strip()
                        vague=('good effort','solid moment','could improve','nice action')
                        if (not 0<=index<len(batch) or confidence<0.48 or len(evidence)<25 or len(coaching)<20 or
                                len(outcome)<3 or any(term in note.lower() for term in vague)): continue
                        second=batch[index][0]
                        score=lambda key:max(1,min(10,int(proposal.get(key,5))))
                        con.execute("INSERT INTO ai_suggestions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",(
                            uid(),match_id,second,proposal['event_type'],outcome,confidence,
                            score('technique'),score('decision_making'),score('positioning'),score('execution'),
                            note,'pending',now()))
                        suggestion_count+=1
                progress=70+round(min(len(sequences),start+len(batch))/max(1,len(sequences))*25)
                update_analysis_job(job_id,'running',progress,'Reviewing actions across before-and-after frames')
        if suggestion_count:
            noun='suggestion' if suggestion_count==1 else 'suggestions'
            message=f'{suggestion_count} AI {noun} ready for coach review'
        else:
            message='Scan complete — no confident goalkeeper events found'
        update_analysis_job(job_id,'complete',100,message)
    except Exception as exc:
        message=str(exc)
        logger.exception('AI analysis failed job_id=%s match_id=%s',job_id,match_id)
        lower=message.lower()
        if '401' in lower or '403' in lower or 'authentication' in lower or 'api key' in lower:
            message='AI authentication failed. The configured video-analysis API key needs attention.'
        elif '429' in lower or 'rate_limit' in lower or 'quota' in lower:
            message='AI usage limit reached. Check OpenAI billing and usage limits.'
        elif 'uploaded footage' not in lower and 'duration' not in lower and 'connect to openai' not in lower:
            message='AI analysis failed. The detailed error has been recorded in the service logs.'
        update_analysis_job(job_id,'failed',0,message)

@app.post('/api/matches/{match_id}/ai-analysis')
def start_ai_analysis(match_id:str,x:AIAnalysisStart,background_tasks:BackgroundTasks,user=Depends(get_user)):
    description=x.goalkeeper_description.strip()
    if len(description)<8: raise HTTPException(400,'Describe the goalkeeper using shirt colour, number or other visible details')
    with db() as con:
        match=match_owned(con,match_id,user['id'])
        if not match['video_path']: raise HTTPException(400,'Upload footage before running AI analysis')
        con.execute("INSERT INTO match_ai_config(match_id,goalkeeper_description,updated_at) VALUES(?,?,?) ON CONFLICT(match_id) DO UPDATE SET goalkeeper_description=excluded.goalkeeper_description,updated_at=excluded.updated_at",(match_id,description,now()))
        stale_before=(datetime.now(timezone.utc)-timedelta(minutes=30)).isoformat()
        con.execute(
            "UPDATE analysis_jobs SET status='failed',progress=0,message=?,updated_at=? "
            "WHERE match_id=? AND status IN ('queued','running') AND updated_at<?",
            ('The scan stopped unexpectedly. Please start it again.',now(),match_id,stale_before))
        active=con.execute("SELECT id FROM analysis_jobs WHERE match_id=? AND status IN ('queued','running')",(match_id,)).fetchone()
        if active: return {'job_id':active['id'],'status':'running'}
        job_id=uid(); con.execute("INSERT INTO analysis_jobs VALUES(?,?,?,?,?,?,?)",(job_id,match_id,'queued',0,'Queued',now(),now()))
    background_tasks.add_task(run_ai_analysis,job_id,match_id)
    return {'job_id':job_id,'status':'queued'}

@app.get('/api/matches/{match_id}/ai-analysis')
def get_ai_analysis(match_id:str,user=Depends(get_user)):
    with db() as con:
        match_owned(con,match_id,user['id'])
        job=con.execute("SELECT * FROM analysis_jobs WHERE match_id=? ORDER BY created_at DESC LIMIT 1",(match_id,)).fetchone()
        suggestions=con.execute("SELECT * FROM ai_suggestions WHERE match_id=? ORDER BY second",(match_id,)).fetchall()
        config=con.execute("SELECT goalkeeper_description FROM match_ai_config WHERE match_id=?",(match_id,)).fetchone()
    return {'job':dict(job) if job else None,'suggestions':[dict(x) for x in suggestions],
            'goalkeeper_description':config['goalkeeper_description'] if config else ''}

@app.post('/api/ai-suggestions/{suggestion_id}/accept')
def accept_ai_suggestion(suggestion_id:str,user=Depends(get_user)):
    with db() as con:
        row=con.execute("SELECT s.* FROM ai_suggestions s JOIN matches m ON m.id=s.match_id JOIN keepers k ON k.id=m.keeper_id WHERE s.id=? AND k.owner_user_id=?",(suggestion_id,user['id'])).fetchone()
        if not row: raise HTTPException(404,'AI suggestion not found')
        if row['status']!='pending': raise HTTPException(409,'Suggestion already reviewed')
        event_id=uid(); con.execute("INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?,?,?)",(event_id,row['match_id'],row['second'],row['event_type'],row['outcome'],row['technique'],row['decision_making'],row['positioning'],row['execution'],row['note'],now()))
        con.execute("UPDATE ai_suggestions SET status='accepted' WHERE id=?",(suggestion_id,))
    return {'ok':True,'event_id':event_id}

@app.post('/api/ai-suggestions/{suggestion_id}/reject')
def reject_ai_suggestion(suggestion_id:str,user=Depends(get_user)):
    with db() as con:
        row=con.execute("SELECT s.id FROM ai_suggestions s JOIN matches m ON m.id=s.match_id JOIN keepers k ON k.id=m.keeper_id WHERE s.id=? AND k.owner_user_id=?",(suggestion_id,user['id'])).fetchone()
        if not row: raise HTTPException(404,'AI suggestion not found')
        con.execute("UPDATE ai_suggestions SET status='rejected' WHERE id=?",(suggestion_id,))
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

