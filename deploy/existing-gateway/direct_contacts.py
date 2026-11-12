"""Materialise DM contacts on the companion; the Pi directory alone cannot decrypt/send DMs."""
import asyncio,json,re,sqlite3,time
from contextlib import contextmanager
from meshcore import EventType

PIN_SECONDS=14*86400

@contextmanager
def _database(path):
 c=sqlite3.connect(path,timeout=10)
 c.execute('CREATE TABLE IF NOT EXISTS direct_contact_pins(id TEXT PRIMARY KEY,name TEXT NOT NULL,last_used REAL NOT NULL)')
 c.execute('CREATE TABLE IF NOT EXISTS radio_contact_snapshots(id TEXT PRIMARY KEY,payload TEXT NOT NULL,captured REAL NOT NULL)')
 try:yield c;c.commit()
 except Exception:c.rollback();raise
 finally:c.close()

def pin_contact(path,key,name):
 if not re.fullmatch('[0-9a-f]{64}',key):raise ValueError('Invalid direct contact public key')
 with _database(path) as c:c.execute('INSERT INTO direct_contact_pins VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,last_used=excluded.last_used',(key,name[:32],time.time()))

def active_pins(path):
 with _database(path) as c:return c.execute('SELECT id,name FROM direct_contact_pins WHERE last_used>?',(time.time()-PIN_SECONDS,)).fetchall()

def _archived(path,key):
 with _database(path) as c:
  row=c.execute('SELECT deleted FROM contacts WHERE id=?',(key,)).fetchone()
 return bool(row is not None and not row[0])

def _snapshot(path,contact):
 with _database(path) as c:c.execute('INSERT OR REPLACE INTO radio_contact_snapshots VALUES(?,?,?)',(contact['public_key'],json.dumps(contact),time.time()))

def archived_contact_type(path,key):
 with _database(path) as c:
  row=c.execute('SELECT role FROM contacts WHERE id=? AND deleted=0',(key,)).fetchone()
 return row[0] if row is not None and row[0] in (1,3) else 1

async def prepare_direct_contact(mc,key,name,capacity,path,archive,contact_type=1):
 """No RF transmission. Verify membership, import if absent, evict only a backed-up unpinned companion/room."""
 key=str(key).lower()
 if contact_type not in (1,3):raise ValueError('Only companion and room contacts can be transferred from the directory')
 if not re.fullmatch('[0-9a-f]{64}',key):raise ValueError('Invalid direct contact public key')
 if key==mc.self_info.get('public_key'):raise ValueError('Cannot send a direct message to this radio itself')
 response=await mc.commands.get_contacts()
 if response.type!=EventType.CONTACTS:raise RuntimeError('Could not verify companion contacts')
 contacts=list(response.payload.values())
 existing=next((c for c in contacts if c.get('public_key')==key),None)
 if existing:
  if existing.get('type') not in (1,3):raise ValueError('Destination is not a direct-message contact')
  await asyncio.to_thread(pin_contact,path,key,name)
  return existing
 if not isinstance(capacity,int) or capacity<=0:raise RuntimeError('Unknown companion capacity; refusing contact replacement')
 if len(contacts)>capacity:raise RuntimeError('Reported companion capacity is inconsistent')
 await archive(contacts)
 await asyncio.to_thread(pin_contact,path,key,name)
 protected={item[0] for item in await asyncio.to_thread(active_pins,path)}
 victim=None
 if len(contacts)>=capacity:
  candidates=sorted((c for c in contacts if c.get('type') in (1,3) and c.get('public_key') not in protected),key=lambda c:(c.get('last_advert',0),c['public_key']))
  for candidate in candidates:
   if await asyncio.to_thread(_archived,path,candidate['public_key']):victim=candidate;break
  if victim is None:raise RuntimeError('Companion full; no safely archived unpinned contact can be replaced')
  # Disable automatic additions before opening a slot, or an arriving advert could immediately refill it.
  if not mc.self_info.get('manual_add_contacts',False):
   response=await mc.commands.set_manual_add_contacts(True)
   if response.type!=EventType.OK:raise RuntimeError('Could not reserve companion contact slot')
   mc.self_info['manual_add_contacts']=True
  await asyncio.to_thread(_snapshot,path,victim)
  response=await mc.commands.remove_contact(bytes.fromhex(victim['public_key']))
  if response.type!=EventType.OK:raise RuntimeError('Companion rejected archived-contact replacement')
  response=await mc.commands.get_contacts()
  if response.type!=EventType.CONTACTS or any(c.get('public_key')==victim['public_key'] for c in response.payload.values()):raise RuntimeError('Could not confirm companion contact removal')
 contact={'public_key':key,'type':contact_type,'flags':0,'out_path':'','out_path_len':-1,'out_path_hash_mode':0,'adv_name':name.encode('utf-8')[:32].decode('utf-8','ignore'),'last_advert':0,'adv_lat':0.0,'adv_lon':0.0}
 try:
  response=await mc.commands.add_contact(contact)
  if response.type!=EventType.OK:raise RuntimeError('Companion rejected direct contact import')
  response=await mc.commands.get_contacts()
  if response.type!=EventType.CONTACTS or not any(c.get('public_key')==key for c in response.payload.values()):raise RuntimeError('Could not confirm direct contact import')
 except Exception:
  if victim is not None:
   response=await mc.commands.get_contacts()
   if response.type==EventType.CONTACTS and len(response.payload)<capacity and not any(c.get('public_key')==key for c in response.payload.values()):
    await mc.commands.add_contact(victim)
  raise
 await archive([contact])
 return contact
