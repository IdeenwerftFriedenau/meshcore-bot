import asyncio,json,time,os,urllib.request,logging,hashlib,sqlite3,re
from pathlib import Path
from admin_sender import poll as poll_admin_outbox
from direct_contacts import prepare_direct_contact,active_pins,archived_contact_type
from radio_pacing import ReceiveFirstSender
from receive_channels import read_receive_channels
from channel_mirror import ChannelMirror,upload_mirror
from meshcore import MeshCore,EventType
from pi_gateway_bridge import GatewayBridge
from radio_bot import RadioInbox,request_answer,split_reply,send_reply_parts,addressed,bot_addressed
CHANNEL_NAMES={"#ideenwerft","#test","#mc-treffen","#warnings","#news","#fragdenbot","#bot"}
REQUIRED_CHANNELS={"#ideenwerft","#test","#warnings","#fragdenbot","#bot"}
MIRROR=ChannelMirror(os.environ.get('CHANNEL_MIRROR_PATH','/var/lib/meshcore-gateway/channel-mirror.sqlite3'))
INBOX=RadioInbox(os.environ.get("INBOX_PATH","/var/lib/meshcore-gateway/inbox.sqlite3"))
ANSWER_URL=os.environ["INGEST_URL"].rsplit("/",1)[0]+"/channel-message"
DIRECT_URL=os.environ["INGEST_URL"].rsplit("/",1)[0]+"/direct-message"
logging.basicConfig(level=logging.INFO,format='%(asctime)s %(message)s')
URL=os.environ['INGEST_URL'];TOKEN=os.environ['INGEST_TOKEN'];PORT=os.environ['SERIAL_PORT']
CONTACT_ARCHIVE_PATH=Path(os.environ.get('CONTACT_ARCHIVE_PATH','/var/lib/meshcore-gateway/contact-directory.sqlite3'))
LOCAL_OUTBOX_PATH=Path(os.environ.get('LOCAL_OUTBOX_PATH','/var/lib/meshcore-gateway/local-outbox.sqlite3'))
metrics={'received_packets':0,'started_at':time.time()};seen={}
addressed=bot_addressed

def local_outbox_claim():
 LOCAL_OUTBOX_PATH.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
 with sqlite3.connect(LOCAL_OUTBOX_PATH,timeout=3) as db:
  db.execute('CREATE TABLE IF NOT EXISTS messages(id TEXT PRIMARY KEY,channel TEXT NOT NULL,text TEXT NOT NULL,status TEXT NOT NULL,created REAL NOT NULL,updated REAL NOT NULL)')
  db.execute('BEGIN IMMEDIATE')
  now=time.time();db.execute("UPDATE messages SET status='uncertain',updated=? WHERE status='sending' AND updated<?",(now,now-300))
  row=db.execute("SELECT id,channel,text FROM messages WHERE status='queued' ORDER BY created LIMIT 1").fetchone()
  if not row:return None
  db.execute("UPDATE messages SET status='sending',updated=? WHERE id=?",(now,row[0]))
  return row

def local_outbox_result(key,status):
 with sqlite3.connect(LOCAL_OUTBOX_PATH,timeout=3) as db:db.execute("UPDATE messages SET status=?,updated=? WHERE id=? AND status='sending'",(status,time.time(),key))

def read_contact_directory():
 CONTACT_ARCHIVE_PATH.parent.mkdir(parents=True,exist_ok=True)
 with sqlite3.connect(CONTACT_ARCHIVE_PATH,timeout=10) as c:
  c.execute('CREATE TABLE IF NOT EXISTS contacts(id TEXT PRIMARY KEY,name TEXT NOT NULL,role INTEGER NOT NULL,updated REAL NOT NULL,deleted INTEGER NOT NULL DEFAULT 0)')
  c.execute('CREATE TABLE IF NOT EXISTS meta(id INTEGER PRIMARY KEY CHECK(id=1),version INTEGER NOT NULL)')
  c.execute('INSERT OR IGNORE INTO meta VALUES(1,0)')
  c.commit()
  c.execute('BEGIN')
  version=c.execute('SELECT version FROM meta WHERE id=1').fetchone()[0]
  rows=[{'id':r[0],'name':r[1],'role':r[2],'updated':r[3],'deleted':bool(r[4])} for r in c.execute('SELECT id,name,role,updated,deleted FROM contacts ORDER BY id')]
  c.commit()
 return version,rows

def archive_radio_contacts(contacts):
 """Keep a local Pi copy of radio contacts before any capacity-based eviction."""
 CONTACT_ARCHIVE_PATH.parent.mkdir(parents=True,exist_ok=True)
 changed=0
 with sqlite3.connect(CONTACT_ARCHIVE_PATH,timeout=10) as c:
  c.execute('CREATE TABLE IF NOT EXISTS contacts(id TEXT PRIMARY KEY,name TEXT NOT NULL,role INTEGER NOT NULL,updated REAL NOT NULL,deleted INTEGER NOT NULL DEFAULT 0)')
  c.execute('CREATE TABLE IF NOT EXISTS meta(id INTEGER PRIMARY KEY CHECK(id=1),version INTEGER NOT NULL)')
  c.execute('INSERT OR IGNORE INTO meta VALUES(1,0)')
  for contact in contacts:
   key=str(contact.get('id','')).lower();role=contact.get('type')
   if role not in (1,3) or len(key)!=64 or any(ch not in '0123456789abcdef' for ch in key):continue
   name=' '.join(str(contact.get('name','Kontakt')).split())[:32] or 'Kontakt'
   now=time.time()
   try:stamp=float(contact.get('advert') or now)
   except (TypeError,ValueError):stamp=now
   if not 0<stamp<=now+120:stamp=now
   cursor=c.execute('''INSERT INTO contacts(id,name,role,updated,deleted) VALUES(?,?,?,?,0)
                       ON CONFLICT(id) DO UPDATE SET name=excluded.name,role=excluded.role,
                       updated=CASE WHEN contacts.updated>excluded.updated+120 THEN excluded.updated ELSE MAX(contacts.updated,excluded.updated) END,deleted=0
                       WHERE contacts.name!=excluded.name OR contacts.role!=excluded.role OR contacts.deleted!=0 OR contacts.updated>excluded.updated+120''',
                    (key,name,role,stamp))
   changed+=cursor.rowcount
  if changed:c.execute('UPDATE meta SET version=version+1 WHERE id=1')
 return changed

def locally_archived(key):
 if not CONTACT_ARCHIVE_PATH.is_file():return False
 with sqlite3.connect(CONTACT_ARCHIVE_PATH,timeout=10) as c:
  row=c.execute('SELECT deleted FROM contacts WHERE id=?',(key,)).fetchone()
 return bool(row and not row[0])

def lookup_archived_contact(prefix):
 if not CONTACT_ARCHIVE_PATH.is_file():return None
 with sqlite3.connect(CONTACT_ARCHIVE_PATH,timeout=10) as c:
  rows=c.execute('SELECT id,name FROM contacts WHERE id LIKE ? AND deleted=0 LIMIT 2',(prefix+'%',)).fetchall()
 return {'id':rows[0][0],'name':rows[0][1]} if len(rows)==1 else None

def radio_contact_list(response,seen):
 contacts=[];nodes=[]
 for n in response.payload.values():
  if n.get('type') not in (1,2,3):continue
  key=str(n.get('public_key','')).lower()
  if len(key)!=64 or any(c not in '0123456789abcdef' for c in key):continue
  name=str(n.get('adv_name','Unbenannter Knoten'))[:80]
  try:stamp=float(max(n.get('last_advert',0),seen.get(key,0)))
  except (TypeError,ValueError):stamp=0
  if stamp<0 or stamp>time.time()+120:stamp=0
  contact={'id':key,'name':name,'type':int(n['type']),'advert':stamp}
  contacts.append(contact)
  lat=n.get('adv_lat');lon=n.get('adv_lon')
  if isinstance(lat,(int,float)) and isinstance(lon,(int,float)) and 52.2<=lat<=52.8 and 12.8<=lon<=13.9:
   nodes.append({'id':key,'name':name,'role':n['type'],'lat':lat,'lon':lon,'advert':stamp})
 return contacts,nodes

def stored_device_contact(candidate):
 contact=dict(candidate)
 contact.setdefault('flags',0);contact.setdefault('out_path','');contact.setdefault('out_path_len',-1)
 contact.setdefault('out_path_hash_mode',0);contact.setdefault('last_advert',0)
 contact.setdefault('adv_lat',0.0);contact.setdefault('adv_lon',0.0)
 return contact
async def load_channel_indices(mc):
 indices=await read_receive_channels(mc);empty=[]
 for idx in range(16):
  try:channel=await mc.commands.get_channel(idx)
  except Exception:continue
  if channel.type!=EventType.CHANNEL_INFO:continue
  name=channel.payload.get('channel_name','')
  if not name:
   empty.append(idx);continue
  if name not in CHANNEL_NAMES:continue
  expected=bytes.fromhex('8b3387e9c5cdea6ac9e5edbaa115cd72') if name=='Public' else hashlib.sha256(name.encode()).digest()[:16]
  if channel.payload.get('channel_secret')!=expected:raise RuntimeError(f'Channel configuration mismatch at slot {idx}')
  indices[idx]=name
  if name=='#bot':logging.info('Channel verified: %s slot=%d',name,idx)
 for name in ('#news','#fragdenbot','#bot'):
  if name in indices.values():continue
  if not empty:raise RuntimeError(f'No free MeshCore channel slot for {name}')
  idx=empty.pop(0)
  result=await mc.commands.set_channel(idx,name)
  if result.type!=EventType.OK:raise RuntimeError(f'Unable to create {name} on MeshCore device')
  channel=await mc.commands.get_channel(idx)
  expected=hashlib.sha256(name.encode()).digest()[:16]
  if channel.type!=EventType.CHANNEL_INFO or channel.payload.get('channel_name')!=name or channel.payload.get('channel_secret')!=expected:raise RuntimeError(f'MeshCore {name} verification failed')
  indices[idx]=name
  logging.info('Channel configured: %s slot=%d',name,idx)
 if not REQUIRED_CHANNELS.issubset(indices.values()):raise RuntimeError('Required channel configuration mismatch')
 return indices

def upload(data):
 request=urllib.request.Request(URL,json.dumps(data).encode(),headers={'Content-Type':'application/json','Authorization':'Bearer '+TOKEN},method='POST')
 with urllib.request.urlopen(request,timeout=10) as response:response.read()
async def main():
 directory_version_sent=-1
 while True:
  mc=None;worker=None;local_bot=None
  try:
   mc=await MeshCore.create_serial(PORT,default_timeout=5,auto_reconnect=False)
   if mc is None:raise RuntimeError('No USB companion response')
   logging.info('MeshCore USB connected')
   command_lock=asyncio.Lock();original_send=mc.commands.send
   async def serial_send(*args,**kwargs):
    async with command_lock:return await original_send(*args,**kwargs)
   mc.commands.send=serial_send
   paced_send=ReceiveFirstSender(mc.commands.send_chan_msg)
   mc.commands.send_chan_msg=paced_send
   channel_indices=await load_channel_indices(mc)
   local_bot=GatewayBridge("/etc/meshcore-ai-bot/config.toml",mc,channel_indices)
   local_bot.start()
   contact_response=await mc.commands.get_contacts()
   if contact_response.type!=EventType.CONTACTS:raise RuntimeError('Contact query failed')
   local_bot.update_contacts(contact_response.payload)
   contacts,nodes=radio_contact_list(contact_response,seen)
   contact_map={contact['id'][:12]:contact for contact in contacts}
   await asyncio.to_thread(archive_radio_contacts,contacts)
   device_info=await mc.commands.send_device_query()
   max_contacts=device_info.payload.get('max_contacts',0) if device_info.type==EventType.DEVICE_INFO else 0
   if max_contacts:logging.info('Automatic contact exchange enabled: %s radio slots; current %s',max_contacts,len(contacts))
   else:logging.warning('Device did not report contact capacity; contacts will be archived but never auto-removed')
   logging.info('Radio configuration: %s', {k:v for k,v in mc.self_info.items() if k in ('radio_freq','radio_bw','radio_sf','radio_cr','manual_add_contacts')})
   async def advert(event):seen[event.payload['public_key']]=time.time()
   async def rx(event):
    paced_send.received()
    metrics['received_packets']+=1;metrics['last_rx']=time.time()
    for k in ['rssi','snr']:
     if isinstance(event.payload.get(k),(int,float)):metrics[k]=event.payload[k]
   candidate_queue=asyncio.Queue(maxsize=200)
   async def new_contact(event):
    try:candidate_queue.put_nowait(dict(event.payload))
    except asyncio.QueueFull:logging.warning('New-contact queue full; candidate not imported')
   mc.subscribe(EventType.ADVERTISEMENT,advert);mc.subscribe(EventType.RX_LOG_DATA,rx);mc.subscribe(EventType.NEW_CONTACT,new_contact)
   async def process_new_contact(candidate):
    nonlocal contacts,nodes,contact_map
    key=str(candidate.get('public_key','')).lower();role=candidate.get('type')
    if role not in (1,3) or len(key)!=64 or any(ch not in '0123456789abcdef' for ch in key):return
    fresh=await mc.commands.get_contacts()
    if fresh.type!=EventType.CONTACTS:logging.warning('Could not refresh radio contacts before exchange; candidate left untouched');return
    contacts,nodes=radio_contact_list(fresh,seen)
    contact_map={item['id'][:12]:item for item in contacts}
    await asyncio.to_thread(archive_radio_contacts,contacts)
    if any(item['id']==key for item in contacts):return
    archive_candidate={'id':key,'name':str(candidate.get('adv_name','Kontakt'))[:32],
                       'type':int(role),'advert':max(candidate.get('last_advert',0),seen.get(key,0))}
    await asyncio.to_thread(archive_radio_contacts,[archive_candidate])
    if not max_contacts:
     logging.warning('Cannot import new contact %s without a verified device capacity',key[:12]);return
    if len(contacts)>=max_contacts:
     if not mc.self_info.get('manual_add_contacts',False):
      setting=await mc.commands.set_manual_add_contacts(True)
      if setting.type!=EventType.OK:logging.warning('Could not enable manual contact management; leaving radio contacts untouched');return
      mc.self_info['manual_add_contacts']=True
     protected={item[0] for item in await asyncio.to_thread(active_pins,CONTACT_ARCHIVE_PATH)}
     eligible=sorted((item for item in contacts if item['id'] not in protected and item['type'] in (1,3) and item['id']!=key and locally_archived(item['id'])),
                     key=lambda item:(item.get('advert') or 0,item['id']))
     if not eligible:
      logging.warning('Radio is full but no safely archived personal contact can be replaced');return
     oldest=eligible[0]
     removed=await mc.commands.remove_contact(bytes.fromhex(oldest['id']))
     if removed.type!=EventType.OK:logging.warning('Could not replace old contact %s',oldest['id'][:12]);return
     refreshed=await mc.commands.get_contacts()
     if refreshed.type!=EventType.CONTACTS:logging.warning('Could not verify radio contact removal; candidate kept in Pi Funkbuch');return
     contacts,nodes=radio_contact_list(refreshed,seen)
     if any(item['id']==oldest['id'] for item in contacts):logging.warning('Removal of %s was not confirmed; candidate kept in Pi Funkbuch',oldest['id'][:12]);return
     contact_map={item['id'][:12]:item for item in contacts}
     logging.info('Replaced oldest Pi-archived radio contact %s with room for a new contact',oldest['id'][:12])
    if len(contacts)>=max_contacts:
     logging.warning('Radio remains full; candidate %s is safely stored on Pi only',key[:12]);return
    added=await mc.commands.add_contact(stored_device_contact(candidate))
    if added.type!=EventType.OK:logging.warning('New contact %s is on Pi but radio import failed',key[:12]);return
    refreshed=await mc.commands.get_contacts()
    if refreshed.type!=EventType.CONTACTS:logging.warning('Radio import response received but contact list could not be verified for %s',key[:12]);return
    contacts,nodes=radio_contact_list(refreshed,seen)
    if any(item['id']==key for item in contacts):
     contact_map={item['id'][:12]:item for item in contacts}
     logging.info('New contact %s archived on Pi and imported to radio',key[:12])
    else:logging.warning('New contact %s remains safely stored on Pi; radio import not confirmed',key[:12])
   async def archive_raw_contacts(raw):
    rows=[{'id':c.get('public_key',''),'name':c.get('adv_name','Kontakt'),'type':c.get('type'),'advert':c.get('last_advert',0)} for c in raw]
    await asyncio.to_thread(archive_radio_contacts,rows)
   async def prepare_dm(key,name,contact_type=None):
    nonlocal contacts,nodes,contact_map
    if contact_type is None:contact_type=await asyncio.to_thread(archived_contact_type,CONTACT_ARCHIVE_PATH,key)
    await prepare_direct_contact(mc,key,name,max_contacts,CONTACT_ARCHIVE_PATH,archive_raw_contacts,contact_type=contact_type)
    fresh=await mc.commands.get_contacts()
    if fresh.type!=EventType.CONTACTS:raise RuntimeError('Could not refresh contacts after DM preparation')
    contacts,nodes=radio_contact_list(fresh,seen);contact_map={c['id'][:12]:c for c in contacts}
   for pinned_key,pinned_name in await asyncio.to_thread(active_pins,CONTACT_ARCHIVE_PATH):
    try:await prepare_dm(pinned_key,pinned_name);logging.info('Direct contact verified on companion: %s',pinned_key[:12])
    except Exception as error:logging.warning('Direct contact preparation failed for %s: %s',pinned_key[:12],str(error))
   try:await asyncio.to_thread(upload,{'nodes':nodes,'contacts':contacts,'connected':True,'receive_channels':[{'slot':slot,'name':name} for slot,name in channel_indices.items()],**metrics})
   except Exception:logging.warning('Initial receive inventory upload unavailable')
   queue=asyncio.Queue(maxsize=100)
   async def received(event):
    paced_send.received()
    local_bot.engine.pacer.received()
    p=event.payload;idx=p.get('channel_idx')
    if idx not in channel_indices:return
    text=p.get('text','');stamp=p.get('sender_timestamp',0)
    if not isinstance(text,str) or not text or type(stamp) is not int:return
    key=INBOX.record(channel_indices[idx],stamp,text)
    if key:
     MIRROR.record(key,channel_indices[idx],stamp,text)
     decision=local_bot.receive(channel_indices[idx],text,stamp)
     INBOX.status(key,"local_"+decision)
   def upload_direct(data):
    request=urllib.request.Request(DIRECT_URL,json.dumps(data).encode(),headers={'Content-Type':'application/json','Authorization':'Bearer '+TOKEN},method='POST')
    with urllib.request.urlopen(request,timeout=10) as response:response.read()
   async def direct_received(event):
    p=event.payload;prefix=str(p.get('pubkey_prefix','')).lower();contact=contact_map.get(prefix)
    if contact is None:contact=await asyncio.to_thread(lookup_archived_contact,prefix)
    if contact is None:contact={}
    text=p.get('text','');stamp=p.get('sender_timestamp',0)
    if not isinstance(text,str) or not text or type(stamp) is not int:return
    try:await asyncio.to_thread(upload_direct,{'contact_id':contact.get('id',prefix),'contact_name':contact.get('name',prefix),'stamp':stamp,'text':text})
    except Exception as e:logging.warning('Direct message upload unavailable: %s',type(e).__name__)
   last_sent_at=0.0
   async def local_dashboard_outbox(last_sent_at):
    row=await asyncio.to_thread(local_outbox_claim)
    if row is None:return last_sent_at
    key,channel,text=row
    idx=next((index for index,name in channel_indices.items() if name==channel),None)
    if idx is None:
     await asyncio.to_thread(local_outbox_result,key,'failed');return last_sent_at
    try:
     remaining=10-(time.monotonic()-last_sent_at)
     if remaining>0:await asyncio.sleep(remaining)
     response=await mc.commands.send_chan_msg(idx,text)
     last_sent_at=time.monotonic()
     status='accepted' if response.type==EventType.OK else 'uncertain'
     logging.info('Local dashboard message %s: %s',channel,status)
    except Exception:
     status='uncertain'
    await asyncio.to_thread(local_outbox_result,key,status)
    return last_sent_at
   async def answer_worker():
    while True:
     key,idx,stamp,text,queued_at=await queue.get()
     try:
      result=await asyncio.to_thread(request_answer,ANSWER_URL,TOKEN,{'channel':channel_indices[idx],'stamp':stamp,'text':text,'queued_at':queued_at})
      reply=result.get('reply')
      if reply and channel_indices[idx] in ('#ideenwerft','#fragdenbot','#bot') and (channel_indices[idx]!='#bot' or addressed(text) or bool(re.search(r'(?<![\w@])#ideenwerft(?:\s+friedenau)?\b',text.split(': ',1)[-1],re.I))):
       # Commit before sending: an uncertain USB acknowledgement must never trigger a duplicate.
       own=str(mc.self_info.get('name',mc.self_info.get('node_name','Ideenwerft Friedenau')))
       parts=split_reply(reply,max(60,min(133,153-len(own.encode()))))
       async def send_part(part):
        nonlocal last_sent_at
        remaining=10-(time.monotonic()-last_sent_at)
        if remaining>0:await asyncio.sleep(remaining)
        sent=await mc.commands.send_chan_msg(idx,part)
        last_sent_at=time.monotonic()
        logging.info('Reply part %s bytes=%s radio_accepted=%s',part.split('] ',1)[0]+']' if part.startswith('[') else '1/1',len(part.encode()),sent.type.name)
        return sent.type==EventType.OK
       INBOX.status(key,'sending',reply)
       try:
        delivered=await send_reply_parts(parts,send_part,asyncio.sleep,lambda part: INBOX.status(key,'sending',reply))
       except Exception:
        delivered=False
       status='radio_accepted' if delivered else 'send_uncertain'
       INBOX.status(key,status,reply)
       try:await asyncio.to_thread(request_answer,ANSWER_URL.rsplit('/',1)[0]+'/channel-delivery',TOKEN,{'id':key,'status':status,'reply':reply})
       except Exception:logging.warning('Delivery status upload unavailable')
       logging.info('Channel %s reply: %s (%s parts)',channel_indices[idx],status,len(parts))
      else:INBOX.status(key,result.get('status','no_reply'))
     except Exception as e:
      INBOX.status(key,'unavailable');logging.warning('Channel processing unavailable: %s',type(e).__name__)
     finally:queue.task_done()
   mc.subscribe(EventType.CHANNEL_MSG_RECV,received)
   mc.subscribe(EventType.CONTACT_MSG_RECV,direct_received)
   async def mirror_worker():
    while True:
     row=MIRROR.next()
     if row is None:
      await asyncio.sleep(0.5);continue
     try:
      await asyncio.to_thread(upload_mirror,ANSWER_URL,TOKEN,row)
      MIRROR.finish(row[0],True)
     except Exception as error:
      MIRROR.finish(row[0],False)
      logging.warning('Receive-only app mirror retry scheduled: %s',type(error).__name__)
   # The local v0.4 engine owns replies; this worker only mirrors received text.
   worker=asyncio.create_task(mirror_worker())
   await mc.start_auto_message_fetching()
   # Drain already queued messages; old mentions are recorded but not answered.
   for _ in range(100):
    pending=await mc.commands.get_msg()
    if pending.type in (EventType.NO_MORE_MSGS,EventType.ERROR):break
   logging.info('Listening: %s; Public receive-only; local v0.4 replies active', ', '.join(channel_indices.values()))
   nodes=[];contacts=[];last_refresh=0
   while True:
    local_bot.check()
    try:last_sent_at=await local_dashboard_outbox(last_sent_at)
    except Exception:logging.warning('Local dashboard outbox unavailable')
    try:last_sent_at=await poll_admin_outbox(mc,URL,TOKEN,last_sent_at,channel_indices,prepare_direct=prepare_dm)
    except Exception:logging.warning("Admin outbox unavailable")
    try:
     candidate=candidate_queue.get_nowait()
     try:await process_new_contact(candidate)
     finally:candidate_queue.task_done()
    except asyncio.QueueEmpty:pass
    if time.time()-last_refresh>30:
     try:
      refreshed_channels=await read_receive_channels(mc,channel_indices)
      if refreshed_channels!=channel_indices:
       await asyncio.to_thread(upload,{'nodes':nodes,'contacts':contacts,'connected':True,'receive_channels':[{'slot':slot,'name':name} for slot,name in refreshed_channels.items()],**metrics})
       channel_indices=refreshed_channels
       local_bot.indices=channel_indices
       logging.info('Receive channels refreshed: %s', ', '.join(channel_indices.values()))
     except Exception as error:
      logging.warning('Receive channel refresh failed; keeping previous inventory: %s',type(error).__name__)
     response=await mc.commands.get_contacts()
     if response.type!=EventType.CONTACTS:raise RuntimeError('Contact query failed')
     local_bot.update_contacts(response.payload)
     contacts,nodes=radio_contact_list(response,seen)
     contact_map={contact['id'][:12]:contact for contact in contacts}
     await asyncio.to_thread(archive_radio_contacts,contacts)
     last_refresh=time.time()
    try:
     directory_version,directory=await asyncio.to_thread(read_contact_directory)
     payload={'nodes':nodes,'contacts':contacts,'connected':True,'receive_channels':[{'slot':slot,'name':name} for slot,name in channel_indices.items()],**metrics}
     if directory_version!=directory_version_sent:payload['directory']=directory
     await asyncio.to_thread(upload,payload)
     if 'directory' in payload:directory_version_sent=directory_version
    except Exception as e:logging.warning('Server upload failed: %s',type(e).__name__)
    await asyncio.sleep(10)
  except Exception as e:logging.warning('USB connection unavailable: %s',type(e).__name__)
  finally:
   if local_bot is not None:
    await local_bot.close()
   if worker is not None:
    worker.cancel()
    try:await worker
    except asyncio.CancelledError:pass
   if mc is not None:
    await mc.stop_auto_message_fetching()
    await mc.disconnect()
  try:await asyncio.to_thread(upload,{'nodes':[],'connected':False,**metrics})
  except Exception:pass
  await asyncio.sleep(10)
asyncio.run(main())
