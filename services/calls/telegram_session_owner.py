"""AnyMsg collector entrypoint with a private call-signaling extension.

Preserves AnyMsg's session lock, governor, capture, credentials and account lease.
No native audio library is loaded into this process.
"""
import os
from pathlib import Path

from messaging_service import telegram_collector as collector
from telegram_broker import SessionBroker


class VoiceProvider(collector.TelethonProvider):
    broker=None

    async def connect(self):
        await super().connect()
        if not self.broker:
            self.broker=SessionBroker(self,os.environ['VOICE_TELEGRAM_SOCKET'],
                Path(os.environ['VOICE_TELEGRAM_TOKEN_FILE']).read_text().strip())
            await self.broker.start()

    async def disconnect(self):
        if self.broker:
            await self.broker.close();self.broker=None
        await super().disconnect()


if __name__=='__main__':
    collector.TelethonProvider=VoiceProvider
    collector.main()
