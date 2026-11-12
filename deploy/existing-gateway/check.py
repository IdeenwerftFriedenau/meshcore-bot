import asyncio
from meshcore import MeshCore
async def main():
 mc=await MeshCore.create_serial('/dev/ttyACM0',default_timeout=3,auto_reconnect=False)
 if mc is None:
  print('RESULT: NO_COMPANION_RESPONSE');return
 try:
  response=await mc.commands.send_device_query()
  print('RESULT:',response.type)
  print('DEVICE_FIELDS:',list(response.payload.keys()))
  print('DEVICE:',{k:v for k,v in response.payload.items() if k in ('fw_ver','firmware_ver','firmware_version','model','ver','build_date','manufacturer','device_model')})
 finally:await mc.disconnect()
asyncio.run(asyncio.wait_for(main(),timeout=18))
