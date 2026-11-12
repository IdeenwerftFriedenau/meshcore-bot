import logging
import asyncio,json,urllib.request,time,sqlite3,os,re
from pathlib import Path
from meshcore import EventType

def archive_contact(message):
 key=str(message.get('recipient','')).lower()
 if not re.fullmatch(r'[0-9a-f]{64}',key):raise ValueError('Invalid contact key for local archive')
 try:role=int(str(message.get('channel','')).rsplit(':',1)[1])
 except (ValueError,IndexError):raise ValueError('Invalid contact type for local archive')
 if role not in (1,3):raise ValueError('Unsupported archived contact type')
 action=message.get('text')
 if action not in ('save','delete'):raise ValueError('Invalid contact archive action')
 name=' '.join(str(message.get('recipient_name','Kontakt')).split())[:32] or 'Kontakt'
 stamp=float(message.get('created') or time.time())
 path=Path(os.environ.get('CONTACT_ARCHIVE_PATH','/var/lib/meshcore-gateway/contact-directory.sqlite3'))
 path.parent.mkdir(parents=True,exist_ok=True)
 with sqlite3.connect(path,timeout=10) as c:
  c.execute('CREATE TABLE IF NOT EXISTS contacts(id TEXT PRIMARY KEY,name TEXT NOT NULL,role INTEGER NOT NULL,updated REAL NOT NULL,deleted INTEGER NOT NULL DEFAULT 0)')
  c.execute('CREATE TABLE IF NOT EXISTS meta(id INTEGER PRIMARY KEY CHECK(id=1),version INTEGER NOT NULL)')
  c.execute('INSERT OR IGNORE INTO meta VALUES(1,0)')
  c.execute('''INSERT INTO contacts(id,name,role,updated,deleted) VALUES(?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET name=excluded.name,role=excluded.role,updated=excluded.updated,deleted=excluded.deleted
               WHERE excluded.updated>=contacts.updated''',(key,name,role,stamp,1 if action=='delete' else 0))
  c.execute('UPDATE meta SET version=version+1 WHERE id=1')

def archived_on_pi(key):
 path=Path(os.environ.get('CONTACT_ARCHIVE_PATH','/var/lib/meshcore-gateway/contact-directory.sqlite3'))
 if not path.is_file():return False
 with sqlite3.connect(path,timeout=10) as c:
  row=c.execute('SELECT deleted FROM contacts WHERE id=?',(key,)).fetchone()
 return bool(row and not row[0])

async def poll(mc,url,token,last_sent,indices,prepare_direct=None):
 def request(path,data):
  req=urllib.request.Request(url.rsplit('/',1)[0]+path,data=json.dumps(data).encode(),headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'})
  with urllib.request.urlopen(req,timeout=8) as r:return json.load(r)
 message=(await asyncio.to_thread(request,'/radio-outbox/claim',{})).get('message')
 if not message:return last_sent
 status='uncertain'
 try:
  kind=message.get('kind')
  if kind=='contact_archive':
   await asyncio.to_thread(archive_contact,message)
   accepted=True
  elif kind=='contact_remove':
   key=str(message.get('recipient','')).lower()
   if not await asyncio.to_thread(archived_on_pi,key):raise ValueError('Contact is not safely archived on the Pi')
   if not mc.self_info.get('manual_add_contacts',False):
    setting=await mc.commands.set_manual_add_contacts(True)
    if setting.type!=EventType.OK:raise ValueError('Could not prevent the radio from automatically re-adding contacts')
    mc.self_info['manual_add_contacts']=True
   response=await mc.commands.remove_contact(bytes.fromhex(key))
   accepted=response.type==EventType.OK
  elif kind=='contact_import':
   if message['channel']=='CONTACT:CARD':
    response=await mc.commands.import_contact(bytes.fromhex(message['recipient']))
    accepted=response.type==EventType.OK
   else:
    contact_type=int(message['channel'].split(':',1)[1]);public_key=message['recipient']
    name=message.get('recipient_name','').encode('utf-8')[:32].decode('utf-8','ignore') or 'Kontakt …'+public_key[-6:]
    if contact_type in (1,3):
     if prepare_direct is None:raise RuntimeError('Verified contact transfer is unavailable')
     await prepare_direct(public_key,name,contact_type)
     accepted=True
    else:
     contact={'public_key':public_key,'type':contact_type,'flags':0,'out_path':'','out_path_len':-1,'out_path_hash_mode':0,'adv_name':name,'last_advert':0,'adv_lat':0.0,'adv_lon':0.0}
     response=await mc.commands.add_contact(contact)
     accepted=response.type==EventType.OK
  elif kind=='direct':
   if prepare_direct is None:raise RuntimeError('Direct contact preparation is unavailable')
   await prepare_direct(message['recipient'],message.get('recipient_name') or 'Kontakt')
   remaining=15-(time.monotonic()-last_sent)
   if remaining>0:await asyncio.sleep(remaining)
   response=await mc.commands.send_msg(bytes.fromhex(message['recipient']),message['text'])
   accepted=response.type==EventType.MSG_SENT
  else:
   idx=next((i for i,name in indices.items() if name==message['channel']),None)
   if idx is None:raise ValueError('Channel is not configured on the radio')
   remaining=15-(time.monotonic()-last_sent)
   if remaining>0:await asyncio.sleep(remaining)
   response=await mc.commands.send_chan_msg(idx,message['text'])
   accepted=response.type==EventType.OK
  if kind not in ('contact_import','contact_archive','contact_remove'):last_sent=time.monotonic()
  if accepted:status='accepted'
  else:logging.warning('Radio action rejected: kind=%s id=%s response=%s payload=%s',kind,message['id'],response.type,response.payload)
 except Exception as error:logging.warning('Radio action failed: kind=%s id=%s error=%s',message.get('kind'),message['id'],str(error))
 # Never resend after an uncertain device acknowledgement or reporting failure.
 await asyncio.to_thread(request,'/radio-outbox/result',{'id':message['id'],'status':status})
 return last_sent
