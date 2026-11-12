import asyncio,collections
from meshcore import MeshCore
async def main():
 mc=await MeshCore.create_serial('/dev/ttyACM0',default_timeout=3)
 try:
  r=await mc.commands.get_contacts();rows=list(r.payload.values())
  print('CONTACT_COUNT',len(rows));print('ROLES',dict(collections.Counter(str(x.get('type')) for x in rows)));print('FIELDS',list(rows[0]) if rows else [])
 finally:await mc.disconnect()
asyncio.run(main())
