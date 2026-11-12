import hashlib,json,re,sqlite3,time,urllib.request,logging
from pathlib import Path
MENTION=re.compile(r'(?<![\w@])@(?:\[ideenwerft(?:\s+friedenau)?\]|ideenwerft(?:\s+friedenau\b)?\b)',re.I)
def question_text(text):
 return MENTION.sub('',text,count=1).lstrip(' ,:').strip()
CHANNELS={'#ideenwerft','#test'}
def identity(channel,stamp,text):
 return hashlib.sha256(json.dumps([channel,int(stamp),text],ensure_ascii=False).encode()).hexdigest()
def addressed(text):
 body=text.split(': ',1)[-1]
 return bool(MENTION.search(body)) and not body.startswith('KI:') and not text.startswith('KI:') and not body.startswith('Ideenwerft:') and not text.startswith('Ideenwerft:') and not text.startswith('Ideenwerft Friedenau:')
def bot_addressed(text):
 body=text.split(': ',1)[-1]
 tags={'#ideenwerft','#ideenwerft-friedenau'}
 return addressed(text) or any(token.strip('.,:;!?()[]{}') in tags for token in body.casefold().split())
def shorten(text,limit=110):
 text=' '.join(str(text).split()).replace('@ideenwerft','Ideenwerft')
 raw=text.encode('utf-8')
 if len(raw)<=limit:return text
 return raw[:max(0,limit-3)].decode('utf-8','ignore').rstrip()+'…'
def split_reply(text,limit=110):
 text=str(text)
 if '\n' in text and len(text.encode())<=limit:return [text]
 if '\n' in text:
  # Preserve multi-line ASCII art across radio packets without prefixes.
  chunks=[];chunk=''
  for line in text.splitlines():
   candidate=(chunk+'\n' if chunk else '')+line
   if chunk and len(candidate.encode())>limit:
    chunks.append(chunk);chunk=line
   else:chunk=candidate
  if chunk:chunks.append(chunk)
  if chunks and all(len(part.encode())<=limit for part in chunks):return chunks
 text=' '.join(text.split())
 # Leave room for part numbers, including three-digit counts.
 size=limit-10
 chunks=[]
 while text:
  if len(text.encode())<=size:chunks.append(text);break
  end=len(text.encode()[:size].decode('utf-8','ignore'))
  space=text.rfind(' ',0,end+1)
  if space>0:end=space
  chunks.append(text[:end]);text=text[end:].lstrip()
 if len(chunks)==1:return chunks
 return [f'[{i}/{len(chunks)}] '+chunk for i,chunk in enumerate(chunks,1)]

async def send_reply_parts(parts,send,sleep,on_part):
 for index,part in enumerate(parts):
  if index:await sleep(15)
  on_part(part)
  if not await send(part):return False
 return True

class RadioInbox:
 def __init__(self,path):
  Path(path).parent.mkdir(parents=True,exist_ok=True)
  self.conn=sqlite3.connect(path)
  self.conn.execute('CREATE TABLE IF NOT EXISTS messages(id TEXT PRIMARY KEY,channel TEXT,stamp INTEGER,text TEXT,state TEXT,reply TEXT,received REAL)')
  self.conn.commit()
 def record(self,channel,stamp,text):
  key=identity(channel,stamp,text)
  with self.conn:
   self.conn.execute('DELETE FROM messages WHERE received<?',(time.time()-86400,))
   cur=self.conn.execute('INSERT OR IGNORE INTO messages VALUES(?,?,?,?,?,?,?)',(key,channel,stamp,text,'received','',time.time()))
  return key if cur.rowcount else None
 def status(self,key,state,reply=''):
  with self.conn:self.conn.execute('UPDATE messages SET state=?,reply=? WHERE id=?',(state,reply,key))
def request_answer(url,token,message):
 req=urllib.request.Request(url,json.dumps(message).encode(),headers={'Content-Type':'application/json','Authorization':'Bearer '+token},method='POST')
 with urllib.request.urlopen(req,timeout=180) as response:return json.load(response)
