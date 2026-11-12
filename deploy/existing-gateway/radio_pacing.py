import asyncio,time,random

class ReceiveFirstSender:
    """One transmit queue for AI and admin; keep receive windows between packets."""
    MIN_GAP_SECONDS=10
    RECEIVE_WINDOW_SECONDS=2
    def __init__(self,send,clock=time.monotonic,sleep=asyncio.sleep,jitter=lambda:random.uniform(.5,1.5)):
        self.send=send;self.clock=clock;self.sleep=sleep;self.jitter=jitter
        self.lock=asyncio.Lock();self.last_sent=None;self.last_rx=None
    def received(self):self.last_rx=self.clock()
    async def __call__(self,*args,**kwargs):
        async with self.lock:
            # Give even the first reply a receive window after its incoming question.
            earliest=self.clock()+self.jitter()
            while True:
                now=self.clock()
                deadline=max(earliest,self.last_sent+self.MIN_GAP_SECONDS if self.last_sent is not None else now,self.last_rx+self.RECEIVE_WINDOW_SECONDS if self.last_rx is not None else now)
                delay=deadline-now
                if delay<=0:break
                await self.sleep(delay)
            try:return await self.send(*args,**kwargs)
            finally:self.last_sent=self.clock()
